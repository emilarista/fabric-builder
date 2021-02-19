# README

Work in progress. Not fully documented thus far. Requirements are python 3.x with some standard modules and the ones listed in requirements.txt.

## Architecture

This fabric-builder script builds configlets for deploying a VXLAN network based on static flood-list configuration with direct routing (All SVIs/VLANs configured on all leafs).

It could be modified to do centralized routing by using the "L2" and "L3" leaf variables that are defined in fabric vars.

## Script structure

The script is structured into different parts to help with readability.

Credentials for CVP are read from a credentials.json file which is included in the repo. ATTENTION The location of the credentials file is hard coded at the beginning of the script, please change to match your environment:

    credentialsfile = "/path/to/my/credentials.json"

### Helper Functions etc.

Importing modules, defining helper functions, etc.

### 1. Start Variable Generation

The variables contained in fabric_vars.json are not enough to easily template a full configuration. There are specifics missing, for example subnet allocation for p2p links etc. So we first have to expand these variables to fit our needs. This is done before any templating can be performed.

### 2. Build Output Folders

We delete the configlet output folder and create a new empty one.

### 3. Render Templates

We take the variables in fabric_vars.json, access_ports.json and overlay.json which have been expanded in step 1 and use them to render the templates into config files and place them in the configlets directory.

Some templates are only rendered once and not for each switch. These are typically overlay related (same for all leafs), or related to common/static config that is applied to some CVP container.

Most templates are rendered once for every switch, like underlay interfaces, ip addressing, etc.

### 4. CVP Interaction Functions

Start defining some functions for interacting with CVP and also start interacting, like building containers, etc.
The CVP interaction uses cvprac module. The script also adds time.sleep() in between API interactions to make it easier on my poor old lab environment.

### 5. Pushing and Attaching Configlets

Grabs config files from the configlets directory and attach them to the proper device or container in CVP. There isn't a 1-1 match between files and configlets, sometimes 2 or more files are joined together to create a single configlet.

## Arguments

    -f --force                  : Force creation of tasks even if no configlets are actually updated by the script. This can be useful if you find yourself in a situation where a configlet is already up to date, but not applied to a device/container for some reason (maybe after a lab environment reset).
    -m --move-to-containers     : Move devices to containers. This only needs to be done once, so no sense in doing it every time we run the script.

## fabric_vars

### Basic stuff
A lot of these vars are pretty simple and self-explanatory, like underlay MTU, mlag VLAN id, etc.

The ones that have some more complex interactions are described in more detail below.

### switch ID
For spines: Mostly an identifier. Also sets the BGP router ID/loopback 0 ip.
For leafs: Number that needs to match the spine interface number unless spine-ports are explicitly identified (not yet supported by the script).

Switch "id" Governs:

* underlay p2p link ip addressing
* loopback 0 ip addressing
* loopback 1 ip addressing
* Tenant "routing VLAN" ip addressing

## Functions

### pushCfglet
This function creates or updates a configlet on CVP. Returns either False or the configlet dict.

1. First it will ALWAYS try to create the configlet, which will fail if it already exists.
2. If it succeeds it returns the configlet dict, to be used when attaching configlets.
3. If it fails, it will try to update the configlet instead. If the function notices there's a change in the configlet contents, it will update the configlet accordingly, and return the configlet dict. Else it will not update and return False, to be used for conditionals outside the function.

### buildContainers
This function builds containers in CVP from the list of containers found in fabric_vars.json.

This function calls itself recursively under some circumstances, but there are safety features in place to keep it from resulting in an infinite loop.

### moveDevicesToContainers
This function moves devices to their proper containers in CVP. Doesn't generally need to be done unless you're adding a new device, so it only executes if the -m flag is given at runtime.

### applyConfiglet
This function applies a configlet to a device or container.

### Enhancements

* Implement sub-leaf underlay logic.
    * Include sub-leafs in template rendering and stuff.
    * Subleaf BGP logic for parent leafs.
    * Add it to flood lists
* Template for underlay BGP relies on a single spine ASN. Make it follow the same logic as downlinks, with neighbor on each link.
* Implement possibility of manual spine-interface definition under leaf uplinks (like for subleafs)
* Implement platform-specific stuff like tcam profile for J2 (not sure it's necessary though).
* Unify template/configlet/file naming so it's consistent.
* Check interface numbering on spines and leafs, make config reflect real life.
* Consider implementing possibility to have different numbered ports per leaf in MLAG. Possibly by passing the switch name or some seq number to the templating.
* Monitor bug BUG399329 for port-channel support for profiles.