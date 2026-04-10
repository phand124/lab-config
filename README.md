Python scripting work for working with Containerlab topologies

src/lab-config/ for scripts. 

Initial thoughts of breakdown, into classes and their methods/roles:
- LabTopology
    - Reads a `clab.yaml` file added as a system argument or input as a directory. 
    - Parses the topology file for node IPs if they exist
    - Confirms topology is running with `clab inspect`
    - Pulls node IPs from the results of `clab inspect` if the topology file did not list them. 

- LabDevice
    - builds a device object
    - Queries `.env` for credentials with `dotenv`
    - binds to kind map
        
- LabConfigurator
    - Loads a device object
    - Loads a connection profile, primarily `netmiko` for now. 
    - Runs the configuration commands
    

The Idea is to build out say run vlans(sw01) and have it build the listed vlans in the clab topology. 
