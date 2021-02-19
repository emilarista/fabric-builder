import json
import ipaddress
import jinja2
import os
import urllib3
import sys
import argparse
import time

from collections import OrderedDict
from pathlib import Path
from cvprac.cvp_client import CvpClient
from cvprac.cvp_client_errors import CvpApiError
from copy import deepcopy

argparser = argparse.ArgumentParser()

argparser.add_argument(
    "-f",
    "--force",
    dest="force",
    action="store_true",
    default=False,
    help="Force task generation",
)

argparser.add_argument(
    "-m",
    "--move-to-containers",
    dest="move_to_containers",
    action="store_true",
    default=False,
    help="Move containers to devices, not done by default to reduce execution time.",
)

options = argparser.parse_args()
force = options.force
move_to_containers = options.move_to_containers

# Don't do this in production, GET AN SSL CERT!!!
urllib3.disable_warnings()

# Instantiate cvprac class
cvpclnt = CvpClient()
credentialsfile = "/path/to/my/credentials.json"
with open(credentialsfile, "r") as f:
    credentials = json.loads(f.read())
    cvp_user = credentials["cvp_user"]
    cvp_password = credentials["cvp_password"]
    cvp_host = credentials["cvp_host"]

cvpclnt.connect([cvp_host], cvp_user, cvp_password)

# Checking if the CVP API is up and we can authenticate.
try:
    result = cvpclnt.api.get_cvp_info()
except Exception as e:
    print("couldn't contact CVP ", e)

# Templating environment setup
templateLoader = jinja2.FileSystemLoader(searchpath="./templates")
templateEnv = jinja2.Environment(loader=templateLoader)

# Opening fabric, overlay, and access port vars .json files.
with open("vars/fabric_vars.json", "r") as f:
    fabric_vars = json.loads(f.read(), object_pairs_hook=OrderedDict)
with open("vars/overlay.json", "r") as f:
    overlay_vars = json.loads(f.read(), object_pairs_hook=OrderedDict)
with open("vars/access_ports.json", "r") as f:
    access_port_vars = json.loads(f.read())

# Helper function for json pretty printing (only used for debug).
def to_json(input):
    return json.dumps(input, indent = 2, sort_keys=True)

# This function would not be necessary if i was better at Jinja2. It clears empty blanklines from .cfg files after templating.
def blankline_strip(inputtext):
    lines = inputtext.split("\n")
    outputtext = ""
    for line in lines:
        if line != "":
            outputtext += line+"\n"
    return outputtext

################################
# 1. START VARIABLE GENERATION #
################################

print("Generating variables...")
# Set some variables
fabric_prefix = fabric_vars["fabric_prefix"]
cfglet_path = "configlets/{}".format(fabric_prefix)
ul_p2p_subnet = ipaddress.ip_network(fabric_vars["underlay_p2p_subnet"])
vtep_subnet = ipaddress.ip_network(fabric_vars["vtep_loopbacks_subnet"])
rid_subnet = ipaddress.ip_network(fabric_vars["rid_loopbacks_subnet"])
routing_vlan_subnet = ipaddress.ip_network(overlay_vars["routing_vlan_subnet"])

# This switch_map dict is used for convenience when we need to reference switches by their IDs...
switch_map = {
    "spines": {},
    "leafs": {},
    "subleafs": {}
}
for spine, info in fabric_vars["switches"]["spines"]["nodes"].items():
    switch_map["spines"][info["id"]] = spine
for nodegroup, contents in fabric_vars["switches"]["leafs"]["node_groups"].items():
    for switch, info in contents["nodes"].items():
        switch_map["leafs"][info["id"]] = switch
for nodegroup, contents in fabric_vars["switches"]["subleafs"]["node_groups"].items():
    for switch, info in contents["nodes"].items():
        switch_map["leafs"][info["id"]] = switch

# quit(ul_p2p_31_subnets)
ul_subnets = list(ul_p2p_subnet.subnets())
ul_p2p_31_subnets = list(ul_subnets[0].subnets(new_prefix = 31))
ul_p2p_subleaf_31_subnets = list(ul_subnets[1].subnets(new_prefix = 31))

# This function builds underlay interface variables from more generalized variables such as switch IDs, p2p supernet, etc.
def underlayBuilder(switch, switchinfo, **kwargs):
    if "sw_type" in kwargs:
        sw_type = kwargs["sw_type"]
    if "subnets" in kwargs:
        ul_p2p_31_subnets = kwargs["subnets"]

    if sw_type == "subleaf":
        counter = 0
        for uplink in switchinfo["uplinks"]:
            leaf_interface = switchinfo["parent_interfaces"][counter]
            link_subnet = ul_p2p_31_subnets[0]
            del ul_p2p_31_subnets[0]
            link_ip = str(link_subnet[-1])
            neighbor_ip = str(link_subnet[0])
            link_neighbor = switchinfo["parent_leafs"][counter]
            # Annoyingly we need to find the parent leaf ASN.
            for nodegrp, contents in fabric_vars["switches"]["leafs"]["node_groups"].items():
                if link_neighbor in contents["nodes"]:
                    neighbor_asn = contents["bgp_asn"]
            bgp_asn = kwargs["bgp_asn"]
            linkdict = {
                "interface": uplink,
                "link_mtu": fabric_vars["underlay_mtu"],
                "link_ip": link_ip,
                "link_neighbor": link_neighbor,
                "neighbor_interface": leaf_interface,
                "neighbor_ip": neighbor_ip,
                "neighbor_asn": neighbor_asn,
                "bgp_asn": bgp_asn
            }
            switchinfo["uplinks"][counter] = linkdict
            counter += 1
        # Setting this for compatibility later
        switchinfo["downlinks"] = []

    elif sw_type == "leaf":
        counter = 0
        for uplink in switchinfo["uplinks"]:
            spine_interface = "Ethernet"+str(switchinfo["id"])
            link_subnet = ul_p2p_31_subnets[0]
            del ul_p2p_31_subnets[0]
            link_ip = str(link_subnet[-1])
            neighbor_ip = str(link_subnet[0])
            link_neighbor = switch_map["spines"][counter+1]
            linkdict = {
                "interface": uplink,
                "link_mtu": fabric_vars["underlay_mtu"],
                "link_ip": link_ip,
                "link_neighbor": link_neighbor,
                "neighbor_interface": spine_interface,
                "neighbor_ip": neighbor_ip
            }
            switchinfo["uplinks"][counter] = linkdict
            counter += 1

        downlinks = []
        for nodegroup, contents in fabric_vars["switches"]["subleafs"]["node_groups"].items():
            for leaf, leafinfo in contents["nodes"].items():
                for link in leafinfo["uplinks"]:
                    if link["link_neighbor"] == switch:
                        link_ip = link["neighbor_ip"]
                        neighbor_ip = link["link_ip"]
                        link_neighbor = leaf
                        interface = link["neighbor_interface"]
                        neighbor_interface = link["interface"]
                        bgp_asn = link["neighbor_asn"]
                        neighbor_asn = link["bgp_asn"]
                        linkdict = {
                            "interface": interface,
                            "link_mtu": fabric_vars["underlay_mtu"],
                            "link_ip": link_ip,
                            "link_neighbor": link_neighbor,
                            "neighbor_interface": neighbor_interface,
                            "neighbor_ip": neighbor_ip,
                            "bgp_asn": bgp_asn,
                            "neighbor_asn": neighbor_asn
                        }
                        downlinks.append(linkdict)

        switchinfo["downlinks"] = downlinks

    elif sw_type == "spine":
        downlinks = []
        for nodegroup, contents in fabric_vars["switches"]["leafs"]["node_groups"].items():
            for leaf, leafinfo in contents["nodes"].items():
                for link in leafinfo["uplinks"]:
                    if link["link_neighbor"] == switch:
                        link_ip = link["neighbor_ip"]
                        neighbor_ip = link["link_ip"]
                        link_neighbor = leaf
                        interface = "Ethernet{}".format(str(leafinfo["id"]))
                        neighbor_interface = link["interface"]
                        linkdict = {
                            "interface": interface,
                            "link_mtu": fabric_vars["underlay_mtu"],
                            "link_ip": link_ip,
                            "link_neighbor": link_neighbor,
                            "neighbor_interface": neighbor_interface,
                            "neighbor_ip": neighbor_ip
                        }
                        downlinks.append(linkdict)

        switchinfo["downlinks"] = downlinks
    return switchinfo

# This function builds loopback interface variables from supernets and switch IDs.
def loopbackBuilder(nodegroup, contents, **kwargs):
    if "sw_type" in kwargs:
        sw_type = kwargs["sw_type"]

    if sw_type == "leaf" or sw_type == "subleaf":
        nodeids = []
        for node, nodeinfo in contents["nodes"].items():
            rid_ip = rid_subnet[nodeinfo["id"]]
            nodeinfo["router_id"] = str(rid_ip)
            nodeids.append(int(nodeinfo["id"]))
            if contents["mlag"] == True:
                vtep_id = min(nodeids)
                vtep_ip = vtep_subnet[vtep_id]
                contents["vtep_ip"] = str(vtep_ip)
            else:
                vtep_ip = vtep_subnet[nodeinfo["id"]]
                nodeinfo["vtep_ip"] = str(vtep_ip)
        return contents

    if sw_type == "spine":
        # This is ugly but it is needed because spines dict doesn't have node_groups
        # and the function input is contents, but it should be called nodeinfo here.
        nodeinfo = contents
        rid_ip = rid_subnet[-nodeinfo["id"]]
        nodeinfo["router_id"] = str(rid_ip)
        return nodeinfo

# Build variables for subleaf underlay interfaces.
for nodegroup, contents in fabric_vars["switches"]["subleafs"]["node_groups"].items():
    for switch, switchinfo in contents["nodes"].items():
        switchinfo = underlayBuilder(switch, switchinfo, sw_type = "subleaf", subnets = ul_p2p_subleaf_31_subnets, bgp_asn = contents["bgp_asn"])

# Build variables for leaf underlay interfaces.
for nodegroup, contents in fabric_vars["switches"]["leafs"]["node_groups"].items():
    for switch, switchinfo in contents["nodes"].items():
        switchinfo = underlayBuilder(switch, switchinfo, sw_type = "leaf", subnets = ul_p2p_31_subnets)

# Build variables for leaf underlay interfaces, based on leaf underlay interfaces.
for switch, switchinfo in fabric_vars["switches"]["spines"]["nodes"].items():
    switchinfo = underlayBuilder(switch, switchinfo, sw_type = "spine")

# Build leaf MLAG vars.
mlag_subnet = ipaddress.ip_network(fabric_vars["mlag_subnet"])
for nodegroup, contents in fabric_vars["switches"]["leafs"]["node_groups"].items():
    if "mlag" in contents:
        mlag = contents["mlag"]
    elif len(contents["nodes"]) == 2:
        mlag = True
        contents["mlag"] = mlag
    else:
        mlag = False
        contents["mlag"] = mlag

    ids = []
    if mlag == True:
        for switch, switchinfo in contents["nodes"].items():
            ids.append(switchinfo["id"])

        peerlink_ifnumbers = []
        for interface in contents["peerlink_interfaces"]:
            interface = interface.lower()
            interface = interface.replace("ethernet", "")
            interface = interface.replace("/", "")
            peerlink_ifnumbers.append(int(interface))

        mlag_po_number = min(peerlink_ifnumbers)
        contents["mlag_peerlink_portchannel"] = mlag_po_number

        for switch, switchinfo in contents["nodes"].items():
            if min(ids) == switchinfo["id"]:
                switchinfo["mlag_ip"] = str(mlag_subnet[0])
                switchinfo["mlag_peer_ip"] = str(mlag_subnet[-1])
            else:
                switchinfo["mlag_ip"] = str(mlag_subnet[-1])
                switchinfo["mlag_peer_ip"] = str(mlag_subnet[0])

# MLAG currently unsupported on subleafs, if needed, copy above for subleafs or make it into a function
for nodegroup, contents in fabric_vars["switches"]["subleafs"]["node_groups"].items():
    contents["mlag"] = False

# Build vtep and router ID loopbacks for subleafs.
for nodegroup, contents in fabric_vars["switches"]["subleafs"]["node_groups"].items():
    contents = loopbackBuilder(nodegroup, contents, sw_type = "subleaf")

# Build vtep and router ID loopbacks for leafs.
for nodegroup, contents in fabric_vars["switches"]["leafs"]["node_groups"].items():
    contents = loopbackBuilder(nodegroup, contents, sw_type = "leaf")

# Build router ID loopbacks for spines.
for node, nodeinfo in fabric_vars["switches"]["spines"]["nodes"].items():
    nodeinfo = loopbackBuilder(node, nodeinfo, sw_type = "spine")

# Build acceptable leafs asn range for spine BGP config.
leafs_asn_range = []
for nodegroup, contents in fabric_vars["switches"]["leafs"]["node_groups"].items():
    if "bgp_asn" in contents:
        leafs_asn_range.append(contents["bgp_asn"])
    for switch, switchinfo in contents["nodes"].items():
        if "bgp_asn" in switchinfo:
            leafs_asn_range.append(switchinfo["bgp_asn"])

leafs_asn_range = "{}-{}".format(str(min(leafs_asn_range)), str(max(leafs_asn_range)))

# Build routing vlans for each VRF and BGP config
routing_vlan_range = overlay_vars["routing_vlan_range"].split("-")
routing_vlan_range = list(range(int(routing_vlan_range[0]), int(routing_vlan_range[-1])))
for vrf, vrfinfo in overlay_vars["vrfs"].items():
    vrf_routing_vlan = str(routing_vlan_range.pop(0))
    vrfinfo["l3vlans"][vrf_routing_vlan] = {
        "tags": ["vrf_routing_vlan"],
        "name": "{}_ROUTING_VLAN".format(vrf),
        "enabled": True,
        "nodes_ips": {}
    }

# Build BGP vrf configuration for leafs and subleafs, notice the nice "concat" of the 2 dicts...
for nodegroup, contents in {**fabric_vars["switches"]["leafs"]["node_groups"], **fabric_vars["switches"]["subleafs"]["node_groups"]}.items():
    for switch, switchinfo in contents["nodes"].items():
        switchinfo["vrfs"] = {}
        for vrf in overlay_vars["vrfs"]:
            switchinfo["vrfs"][vrf] = {
                "neighbors": []
                }

            for switch_id in {**switch_map["leafs"], **switch_map["subleafs"]}:
                if switch_id != switchinfo["id"]:
                    neighbor_ip = str(routing_vlan_subnet[int(switch_id)])
                    switchinfo["vrfs"][vrf]["neighbors"].append(neighbor_ip)

# Routing VLANs for leafs + subleafs
for vrf, vrfinfo in overlay_vars["vrfs"].items():
    for vlan, vlaninfo in vrfinfo["l3vlans"].items():
        if "vrf_routing_vlan" in vlaninfo["tags"] and "{}_ROUTING_VLAN".format(vrf) in vlaninfo["name"]:
            vlaninfo["nodes"] = {}
            masklength = str(routing_vlan_subnet).split("/")[1]
            for nodegroup, contents in fabric_vars["switches"]["leafs"]["node_groups"].items():
                for switch, switchinfo in contents["nodes"].items():
                    svi_ip = routing_vlan_subnet[switchinfo["id"]]
                    vlaninfo["nodes"][switch] = "{}/{}".format(str(svi_ip), masklength)
            for nodegroup, contents in fabric_vars["switches"]["subleafs"]["node_groups"].items():
                for switch, switchinfo in contents["nodes"].items():
                    svi_ip = routing_vlan_subnet[switchinfo["id"]]
                    vlaninfo["nodes"][switch] = "{}/{}".format(str(svi_ip), masklength)

# MLAG port-channel numbers
for server, serverinfo in access_port_vars["servers"].items():
    if "mlag" in serverinfo:
        if serverinfo["mlag"] == True:
            portnumbers = []
            for port in serverinfo["ports"]:
                portnumbers.append(int(port.replace("Ethernet", "").replace("/", "")))
            mlag_number = str(min(portnumbers))
            serverinfo["mlag"] = mlag_number
        else:
            mlag_number = str(serverinfo["mlag"])

for entry in access_port_vars["port_ranges"]:
    port_range = entry["ports"]
    port_range = port_range.replace("Ethernet", "")
    port_range = port_range.split("-")
    for num in range(int(port_range[0]), 1+int(port_range[-1])):
        for leaf in entry["leafs"]:
            server_name = leaf+"_Ethernet"+str(num)
            access_port_vars["servers"][server_name] = {}
            access_port_vars["servers"][server_name]["leafs"] = leaf
            access_port_vars["servers"][server_name]["ports"] = ["Ethernet"+str(num)]
            access_port_vars["servers"][server_name]["profile"] = entry["profile"]
            access_port_vars["servers"][server_name]["description"] = entry["description"]

###########################
# 2. BUILD OUTPUT FOLDERS #
###########################
print("Resetting output folder...")

# Wee bit dangerous!
os.system("rm -r configlets")
Path(cfglet_path).mkdir(parents=True, exist_ok=True)

#########################
# 3. TEMPLATE RENDERING #
#########################
print("Rendering templates...")

# For some rendering it will be nice to have just 1 dict of all leafs and subleafs.
all_leafs_and_subleafs = deepcopy(fabric_vars["switches"]["leafs"]["node_groups"])
all_leafs_and_subleafs.update(fabric_vars["switches"]["subleafs"]["node_groups"])

# Common stuff for all switches
template = templateEnv.get_template("all_common.j2")
fabric_wide_config = template.render(
    user_accounts = fabric_vars["user_accounts"],
    aliases = fabric_vars["aliases"],
    terminattr_string = fabric_vars["terminattr_string"],
    ns_1 = fabric_vars["ns_1"],
    ns_2 = fabric_vars["ns_1"],
    ntp_1 = fabric_vars["ntp_1"],
    ntp_2 = fabric_vars["ntp_2"],
    mgmt_gw = fabric_vars["mgmt_gw"]
)
fabric_wide_config = blankline_strip(fabric_wide_config)
with open("{}/all_common.cfg".format(cfglet_path), "w") as f:
    f.write(fabric_wide_config)

# Common stuff for leafs
template = templateEnv.get_template("leaf_common.j2")
all_leafs_config = template.render(
    leaf_stp_mode = fabric_vars["leaf_stp_mode"]
)
with open("{}/leafs_common.cfg".format(cfglet_path), "w") as f:
    f.write(all_leafs_config)

# Hostname template leafs/subleafs
template = templateEnv.get_template("all_hostname_mgmt_ip.j2")
for nodegroup, contents in all_leafs_and_subleafs.items():
    for switch, switchinfo in contents["nodes"].items():
        hostname_mgmt_config = template.render(
            hostname = switch,
            mgmt_ip = switchinfo["mgmt_ip"]
        )
        with open("{}/{}_hostname_mgmt_ip.cfg".format(cfglet_path, switch), "w") as f:
            f.write(hostname_mgmt_config)

# Hostname template spines
template = templateEnv.get_template("all_hostname_mgmt_ip.j2")
for switch, switchinfo in fabric_vars["switches"]["spines"]["nodes"].items():
    hostname = template.render(
        hostname = switch,
        mgmt_ip = switchinfo["mgmt_ip"]
    )
    with open("{}/{}_hostname_mgmt_ip.cfg".format(cfglet_path, switch), "w") as f:
        f.write(hostname)

# Underlay interfaces leafs/subleafs.
template = templateEnv.get_template("underlay_interfaces.j2")
for nodegroup, contents in all_leafs_and_subleafs.items():
    for switch, switchinfo in contents["nodes"].items():
        interface_config_text = ""
        for interface in switchinfo["uplinks"]:
            interface_config_text += template.render(interface)
        for interface in switchinfo["downlinks"]:
            interface_config_text += template.render(interface)
        with open("{}/{}_underlay_interfaces.cfg".format(cfglet_path, switch), "w") as f:
            f.write(interface_config_text)

# Underlay interfaces spines.
template = templateEnv.get_template("underlay_interfaces.j2")
for switch, switchinfo in fabric_vars["switches"]["spines"]["nodes"].items():
    interface_config_text = ""
    for interface in switchinfo["downlinks"]:
        interface_config_text += template.render(interface)
    with open("{}/{}_underlay_interfaces.cfg".format(cfglet_path, switch), "w") as f:
        f.write(interface_config_text)

# Underlay BGP for spines.
template = templateEnv.get_template("spine_underlay_bgp.j2")
spine_bgp_asn = fabric_vars["switches"]["spines"]["base_asn"]
for switch, switchinfo in fabric_vars["switches"]["spines"]["nodes"].items():
    if "bgp_asn" in switchinfo:
        spine_bgp_asn = switchinfo["bgp_asn"]

    spine_bgp_config = template.render(
        router_id = switchinfo["router_id"],
        leafs_asn_range = leafs_asn_range,
        bgp_asn = spine_bgp_asn,
        bgp_md5 = fabric_vars["bgp_md5"],
        underlay_p2p_subnet = fabric_vars["underlay_p2p_subnet"]
    )
    spine_bgp_config = blankline_strip(spine_bgp_config)
    with open("{}/{}_underlay_bgp.cfg".format(cfglet_path, switch), "w") as f:
        f.write(spine_bgp_config)

# Underlay BGP for leafs/subleafs.
template = templateEnv.get_template("leaf_underlay_bgp.j2")
for nodegroup, contents in all_leafs_and_subleafs.items():
    if "bgp_asn" in contents:
        leaf_bgp_asn = contents["bgp_asn"]
    for switch, switchinfo in contents["nodes"].items():
        if "bgp_asn" in switchinfo:
            leaf_bgp_asn = switchinfo["bgp_asn"]

        leaf_bgp_config = template.render(
            bgp_asn = leaf_bgp_asn,
            uplinks = switchinfo["uplinks"],
            downlinks = switchinfo["downlinks"],
            router_id = switchinfo["router_id"],
            bgp_md5 = fabric_vars["bgp_md5"],
            spine_asn = fabric_vars["switches"]["spines"]["base_asn"]
        )
        leaf_bgp_config = blankline_strip(leaf_bgp_config)
        with open("{}/{}_underlay_bgp.cfg".format(cfglet_path, switch), "w") as f:
            f.write(leaf_bgp_config)



# MLAG for MLAG leafs
template = templateEnv.get_template("leaf_mlag.j2")
for nodegroup, contents in fabric_vars["switches"]["leafs"]["node_groups"].items():
    if "mlag" in contents and contents["mlag"] == True:
        for switch, switchinfo in contents["nodes"].items():
            mlag_config = template.render(
                mlag_vlan = fabric_vars["mlag_vlan"],
                underlay_mtu = fabric_vars["underlay_mtu"],
                mlag_ip = switchinfo["mlag_ip"],
                peerlink_interfaces = contents["peerlink_interfaces"],
                mlag_peerlink_portchannel = contents["mlag_peerlink_portchannel"],
                mlag_domain = nodegroup,
                mlag_peer_ip = switchinfo["mlag_peer_ip"]
            )
            mlag_config = blankline_strip(mlag_config)
            with open("{}/{}_mlag.cfg".format(cfglet_path, switch), "w") as f:
                f.write(mlag_config)

# VXLAN VTEP configuration, all VTEPs (leafs+subleafs)
template = templateEnv.get_template("leaf_vtep.j2")
for nodegroup, contents in all_leafs_and_subleafs.items():
    if "vtep_ip" in contents:
        vtep_ip = contents["vtep_ip"]
    for switch, switchinfo in contents["nodes"].items():
        if "vtep_ip" in switchinfo:
            vtep_ip = switchinfo["vtep_ip"]

        vtep_config = template.render(
            vtep_ip = vtep_ip
        )
        with open("{}/{}_vtep_configuration.cfg".format(cfglet_path, switch), "w") as f:
            f.write(vtep_config)

# VXLAN Overlay configuration, all VTEPs (leafs+subleafs)
flood_list = []
template = templateEnv.get_template("leaf_vxlan.j2")
for nodegroup, contents in all_leafs_and_subleafs.items():
    if "vtep_ip" in contents:
        flood_list.append(contents["vtep_ip"])

    for switch, switchinfo in contents["nodes"].items():
        if "vtep_ip" in switchinfo:
            flood_list.append(switchinfo["vtep_ip"])

vlan_ids = []
for vlan in overlay_vars["l2vlans"]:
    vlan_ids.append(int(vlan))

for vrf, vrfinfo in overlay_vars["vrfs"].items():
    for vlan in vrfinfo["l3vlans"]:
        vlan_ids.append(int(vlan))
        min_vlan = min(vlan_ids)
        max_vlan = max(vlan_ids)
        min_vni = overlay_vars["base_vni"] + min_vlan
        max_vni = overlay_vars["base_vni"] + max_vlan

leafs_vxlan_config = template.render(
    flood_list = flood_list,
    min_vlan = min_vlan,
    max_vlan = max_vlan,
    min_vni = min_vni,
    max_vni = max_vni
)
leafs_vxlan_config = blankline_strip(leafs_vxlan_config)
with open("{}/leafs_vxlan_configuration.cfg".format(cfglet_path), "w") as f:
    f.write(leafs_vxlan_config)

# Build and render vlans
all_vlans = {}
for vlan, vlaninfo in overlay_vars["l2vlans"].items():
    all_vlans[vlan] = vlaninfo

for vrf, vrfinfo in overlay_vars["vrfs"].items():
    for vlan, vlaninfo in vrfinfo["l3vlans"].items():
        if "vrf_routing_vlan" not in vlaninfo["tags"]:
            all_vlans[vlan] = vlaninfo
            all_vlans[vlan]["vrf"] = vrf

template = templateEnv.get_template("leaf_vlans_svis.j2")
vlan_config = template.render(
    vlans = all_vlans
)

# Render vrfs
template = templateEnv.get_template("leaf_vrfs.j2")
vrf_config = template.render(
    vrfs = overlay_vars["vrfs"]
)
overlay_config = vrf_config + vlan_config
overlay_config = blankline_strip(overlay_config)
with open("{}/leafs_overlay_vlans_svis_vrfs.cfg".format(cfglet_path), "w") as f:
    f.write(overlay_config)

# Render vrf routing vlans
template = templateEnv.get_template("leaf_vrf_routing_vlans.j2")
for nodegrp, contents in all_leafs_and_subleafs.items():
    for switch, switchinfo in contents["nodes"].items():
        routing_vlans_config = ""
        for vrf, vrfinfo in overlay_vars["vrfs"].items():
            for vlan, vlaninfo in vrfinfo["l3vlans"].items():
                if "vrf_routing_vlan" in vlaninfo["tags"] and "{}_ROUTING_VLAN".format(vrf) in vlaninfo["name"]:
                    svi_ip = vlaninfo["nodes"][switch]
                    routing_vlans_config += "\n" + template.render(
                        vlan = vlan,
                        svi_ip = svi_ip,
                        vrf = vrf,
                        name = vlaninfo["name"]
                    )
                    break
        routing_vlans_config = blankline_strip(routing_vlans_config)
        with open("{}/{}_vrf_routing_vlans.cfg".format(cfglet_path, switch), "w") as f:
            f.write(routing_vlans_config)

# BGP overlay configuration
template = templateEnv.get_template("leaf_overlay_bgp.j2")
for nodegroup, contents in all_leafs_and_subleafs.items():
    if "bgp_asn" in contents:
        bgp_asn = contents["bgp_asn"]
    for switch, switchinfo in contents["nodes"].items():
        if "bgp_asn" in switchinfo:
            bgp_asn = switchinfo["bgp_asn"]

        vrfs = switchinfo["vrfs"]
        local_asn = overlay_vars["overlay_asn"]
        bgp_md5 = fabric_vars["bgp_md5"]

        leaf_overlay_bgp_config = template.render(
            bgp_asn = bgp_asn,
            vrfs = vrfs,
            local_asn = local_asn,
            bgp_md5 = bgp_md5
        )
        leaf_overlay_bgp_config = blankline_strip(leaf_overlay_bgp_config)
        with open("{}/{}_overlay_bgp.cfg".format(cfglet_path, switch), "w") as f:
            f.write(leaf_overlay_bgp_config)

# Access port profiles
template = templateEnv.get_template("leaf_access_port_profiles.j2")
profiles_config = template.render(
    profiles = access_port_vars["port_profiles"]
)
profiles_config = blankline_strip(profiles_config)
with open("{}/leafs_access_port_profiles.cfg".format(cfglet_path), "w") as f:
    f.write(profiles_config)

# Access ports, gonna have to run one for mlag and one for non-mlag
# Should implement some check for reserved interfaces... Maybe.
# At the moment, only the first interface in the ports list is used when rendering the mlag template, so in effect - you can't have different ports on different leafs in the mlag,
# The ports used need to be named and numbered exactly the same.
mlag_template = templateEnv.get_template("leaf_access_mlags.j2")
port_template = templateEnv.get_template("leaf_access_ports.j2")
for nodegrp, contents in all_leafs_and_subleafs.items():
    for switch in contents["nodes"]:
        access_mlags = []
        access_ports = []
        for server, serverinfo in access_port_vars["servers"].items():
            if switch in serverinfo["leafs"]:
                serverinfo["name"] = server
                if "mlag" in serverinfo:
                    access_mlags.append(serverinfo)
                else:
                    access_ports.append(serverinfo)
        interface_config = ""
        if len(access_ports) > 0:
            interface_config += port_template.render(
                access_ports = access_ports
            )
        if len(access_mlags) > 0:
            interface_config += "\n"+mlag_template.render(
                access_mlags = access_mlags
            )
        interface_config = blankline_strip(interface_config)
        with open("{}/{}_access_ports.cfg".format(cfglet_path, switch), "w") as f:
            f.write(interface_config)

################################
# 4. CVP INTERACTION FUNCTIONS #
################################

# This function builds containers in CVP. Under certain circumstances it calls itself recursively.
def buildContainers(clnt, containerlist, rerun_count):
    # Safety features since this function calls itself under certain circumstances.
    # Also we have to check the rerun_count since we don't want to create an infinite loop. This can happen if the containerlist has a
    # container whose parent doesn't exist AND the parent is not in the containerlist to be created.
    if len(containerlist) == 0:
        sys.exit()
    if rerun_count == 0:
        sys.exit()
    rerun_count -= 1
    # Create empty list of retries, may be needed for containers whose parents don't yet exist.
    retries = []

    # Loop thru containers to see if they exist already.
    for c in containerlist:
        result = clnt.api.search_topology(c["name"])
        containerexists = False
        if len(result["containerList"]) > 0:
            for item in result["containerList"]:
                if c["name"] == item["name"]:
                    containerexists = True
                    break

        # For containers that don't exist we have to check if the parent containers exists first.
        if containerexists == False:
            parentexists = False
            result = clnt.api.search_topology(c["parent_container"])
            if len(result["containerList"]) > 0:
                for item in result["containerList"]:
                    if c["parent_container"] == item["name"]:
                        parentexists = True
                        parentkey = item["key"]
                        break

            # If the container doesn't yet exist but its parent exists we are free to create the new container.
            if parentexists == True:
                c_name = c["name"]
                parent = c["parent_container"]
                clnt.api.add_container(c_name, parent, parentkey)
            # Else we will have to retry.
            else:
                retries.append(c)

    # If we have retries, meaning we encountered containers in need of creation but whose parents did not exist, we need to run again with those retries.
    # HERE we have the possibility of an infinite loop if a container parent is not itself present in the container list (perhaps due to spelling errors).
    # That's why we have rerun_count, which self-decrements each time the function calls itself recursively.
    if len(retries) > 0:
        buildContainers(cvpclnt, retries, rerun_count)

print("Checking for undeployed containers...")
# Litte test to see if we have undeployed containers
cvp_containers = cvpclnt.api.get_containers()["data"]
containerlist = fabric_vars["cvp_containers"]
undeployed_containers = False
for container in containerlist:
    if undeployed_containers == False:
        for cvp_container in cvp_containers:
            if container["name"] == cvp_container["name"]:
                undeployed_containers = False
                break
            else:
                undeployed_containers = True
    else:
        break

# If we do have undeployed containers, sync containers, maximun retry count of 10 (way overkill here but should suffice for any topology).
if undeployed_containers == True:
    buildContainers(cvpclnt, containerlist, 10)

# Move devices to containers function
def moveDevicesToContainers(clnt, containers, **kwargs):
    if "skips" in kwargs:
        skips = kwargs["skips"]
    else:
        skips = False

    for switch, switchinfo in fabric_vars["switches"]["spines"]["nodes"].items():
        if skips:
            if switch in skips:
                # Skip switchnames that are in skips list.
                continue
        dev_dict = clnt.api.get_device_by_name(switch)
        time.sleep(0.1)
        for container in containers:
            if switchinfo["parent_container"] == container["name"]:
                clnt.api.move_device_to_container("bork", dev_dict, container)
                time.sleep(0.1)
                break

    for nodegrp, contents in all_leafs_and_subleafs.items():
        for switch, switchinfo in contents["nodes"].items():
            if skips:
                if switch in skips:
                    continue
            dev_dict = clnt.api.get_device_by_name(switch)
            time.sleep(0.1)
            print("Moving {} to container.".format(switch))
            for container in containers:
                if switchinfo["parent_container"] == container["name"]:
                    try:
                        clnt.api.move_device_to_container("bork", dev_dict, container)
                        time.sleep(0.1)
                        break
                    except KeyError as e:
                        print("Something went wrong while moving {} to its container: {}. Maybe the device is not provisioned?".format(switch, container["name"]))

if move_to_containers:
    print("Moving devices to containers...")
    moveDevicesToContainers(cvpclnt, cvp_containers)

# CVP helper function to push configlets with exception handling.
def pushCfglet(clnt, cfgtext, cfgletname, **kwargs):
    if "debug" in kwargs:
        debug = kwargs["debug"]
    else:
        debug = False

    try:
        # Try to build the desired configlet
        clnt.api.add_configlet(cfgletname, cfgtext)
        time.sleep(0.1)
        result = clnt.api.get_configlet_by_name(cfgletname)
        time.sleep(0.1)

    except CvpApiError as error:
        # If we fail to build it (because it already exists), update it instead.
        if "Data already exists in Database" in str(error):
            if debug == True:
                print("WARNING: Existing configlet {} overwritten".format(cfgletname))
            result = clnt.api.get_configlet_by_name(cfgletname)
            time.sleep(0.1)
            key = result["key"]
            if cfgtext == result["config"]:
                # We don't update unless there's an actual change in the config contents, or force flag is set.
                if force == True:
                    result = clnt.api.get_configlet_by_name(cfgletname)
                    time.sleep(0.1)
                else:
                    result = False
            else:
                clnt.api.update_configlet(cfgtext, key, cfgletname)
                time.sleep(0.1)
                result = clnt.api.get_configlet_by_name(cfgletname)
                time.sleep(0.1)

    return result

# Function to apply configlet
def applyConfiglet(target, cfglet_dict, **kwargs):
    if "target_type" not in kwargs:
        target_type = "device"
    else:
        target_type = kwargs["target_type"]

    if target_type != "device" and target_type != "container":
        raise Exception("invalid target_type specified. Needs to be 'device' or 'container'")

    new_configlets = []
    new_configlets.append(cfglet_dict)
    try:
        if target_type == "device":
            device_dict = cvpclnt.api.get_device_by_name(target)
            time.sleep(0.1)
            try:
                cvpclnt.api.apply_configlets_to_device("bork", device_dict, new_configlets)
                time.sleep(0.1)
            except KeyError:
                print("There was an error with applying configlets to this device, are you sure it is provisioned? {}".format(switch))
                pass

        elif target_type == "container":
            container_dict = cvpclnt.api.get_container_by_name(target)
            time.sleep(0.1)
            cvpclnt.api.apply_configlets_to_container("bork", container_dict, new_configlets)
            time.sleep(0.1)

    except CvpApiError as error:
        if "Device not reachable" in str(error):
            pass
        else:
            raise Exception("CVP Threw this unexpected error while trying to attach configlets {} to device {}".format(cfglet_name, switch), str(error))

####################################
# PUSHING AND ATTACHING CONFIGLETS #
####################################
print("Pushing and attaching cvp configlets...")

cfglet_prefix = "{}_".format(fabric_prefix)
# Just making a list for convenience.
all_switches = []
for switch in fabric_vars["switches"]["spines"]["nodes"]:
    all_switches.append(switch)
for nodegrp, contents in all_leafs_and_subleafs.items():
    for switch in contents["nodes"]:
        all_switches.append(switch)

# all_common
with open("{}/all_common.cfg".format(cfglet_path), "r") as f:
    cfglet_name = "{}all_common".format(cfglet_prefix)
    cfglet_text = f.read()
    cfglet = pushCfglet(cvpclnt, cfglet_text, cfglet_name)
    if cfglet != False:
        applyConfiglet("DEMO_DC", cfglet, target_type = "container")

# leaf common
with open("{}/leafs_common.cfg".format(cfglet_path), "r") as f:
    cfglet_name = "{}leaf_common".format(cfglet_prefix)
    cfglet_text = f.read()
    cfglet = pushCfglet(cvpclnt, cfglet_text, cfglet_name)
    if cfglet != False:
        applyConfiglet("LEAFS", cfglet, target_type = "container")

# mgmt ip + hostname
for switch in all_switches:
    with open("{}/{}_hostname_mgmt_ip.cfg".format(cfglet_path, switch), "r") as f:
        cfglet_text = f.read()

    cfglet_name = "{}_hostname_mgmt_ip".format(switch)
    cfglet = pushCfglet(cvpclnt, cfglet_text, cfglet_name)
    if cfglet != False:
        applyConfiglet(switch, cfglet)

# underlay bgp + interfaces
for switch in all_switches:
    with open("{}/{}_underlay_interfaces.cfg".format(cfglet_path, switch), "r") as f:
        ul_interfaces = f.read()
    with open("{}/{}_underlay_bgp.cfg".format(cfglet_path, switch), "r") as f:
        ul_bgp = f.read()

    cfglet_text = "{}\n!\n{}".format(ul_interfaces, ul_bgp)
    cfglet_name = "{}{}_underlay_interfaces_bgp".format(cfglet_prefix, switch)
    cfglet = pushCfglet(cvpclnt, cfglet_text, cfglet_name)
    if cfglet != False:
        applyConfiglet(switch, cfglet)

# Leaf VTEP
for nodegrp, contents in all_leafs_and_subleafs.items():
    for switch in contents["nodes"]:
        with open("{}/{}_vtep_configuration.cfg".format(cfglet_path, switch), "r") as f:
            cfglet_text = f.read()

        cfglet_name = "{}{}_vtep_configuration".format(cfglet_prefix, switch)
        cfglet = pushCfglet(cvpclnt, cfglet_text, cfglet_name)
        if cfglet != False:
            applyConfiglet(switch, cfglet)

# Leafs VXLAN config
with open("{}/leafs_vxlan_configuration.cfg".format(cfglet_path), "r") as f:
    cfglet_text = f.read()
cfglet_name = "{}leafs_interface_vxlan".format(cfglet_prefix)
cfglet = pushCfglet(cvpclnt, cfglet_text, cfglet_name)
if cfglet != False:
    applyConfiglet("LEAFS", cfglet, target_type = "container")

# MLAG config
for nodegrp, contents in all_leafs_and_subleafs.items():
    if contents["mlag"] == True:
        for switch in contents["nodes"]:
            with open("{}/{}_mlag.cfg".format(cfglet_path, switch), "r") as f:
                cfglet_text = f.read()
            cfglet_name = "{}{}_mlag_configuration".format(cfglet_prefix, switch)
            cfglet = pushCfglet(cvpclnt, cfglet_text, cfglet_name)
            if cfglet != False:
                applyConfiglet(switch, cfglet)

# Overlay VLANs/SVIs/VRFs
with open("{}/leafs_overlay_vlans_svis_vrfs.cfg".format(cfglet_path), "r") as f:
    cfglet_text = f.read()
cfglet_name = "{}leafs_overlay_vlans_svis_vrfs".format(cfglet_prefix)
cfglet = pushCfglet(cvpclnt, cfglet_text, cfglet_name)
if cfglet != False:
    applyConfiglet("LEAFS", cfglet, target_type = "container")

# VRF routing vlans
for nodegrp, contents in all_leafs_and_subleafs.items():
    for switch in contents["nodes"]:
        with open("{}/{}_vrf_routing_vlans.cfg".format(cfglet_path, switch), "r") as f:
            cfglet_text = f.read()
        cfglet_name = "{}{}_vrf_routing_vlans".format(cfglet_prefix, switch)
        cfglet = pushCfglet(cvpclnt, cfglet_text, cfglet_name)
        if cfglet != False:
            applyConfiglet(switch, cfglet)

# Overlay iBGP peerings
for nodegrp, contents in all_leafs_and_subleafs.items():
    for switch in contents["nodes"]:
        with open("{}/{}_overlay_bgp.cfg".format(cfglet_path, switch), "r") as f:
            cfglet_text = f.read()
        cfglet_name = "{}{}_overlay_ibgp_peering".format(cfglet_prefix, switch)
        cfglet = pushCfglet(cvpclnt, cfglet_text, cfglet_name)
        if cfglet != False:
            applyConfiglet(switch, cfglet)

# Port profiles
with open("{}/leafs_access_port_profiles.cfg".format(cfglet_path), "r") as f:
    cfglet_text = f.read()
cfglet_name = "{}leafs_access_port_profiles".format(cfglet_prefix)
cfglet = pushCfglet(cvpclnt, cfglet_text, cfglet_name)
if cfglet != False:
    applyConfiglet("LEAFS", cfglet, target_type = "container")

# Access ports
for nodegrp, contents in all_leafs_and_subleafs.items():
    for switch in contents["nodes"]:
        with open("{}/{}_access_ports.cfg".format(cfglet_path, switch), "r") as f:
            cfglet_text = f.read()
        cfglet_name = "{}{}_access_ports".format(cfglet_prefix, switch)
        cfglet = pushCfglet(cvpclnt, cfglet_text, cfglet_name)
        if cfglet != False:
            applyConfiglet(switch, cfglet)


print("Done!")