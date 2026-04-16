# LabTopology Class and method definition. 
# Loads a ContainerLab topology with yaml and pulls information

class LabTopology:

    def __init__(self, topology_file=None):
        from sys import argv
        if topology_file:
            self.topology_file = topology_file
        elif len(argv) > 1:
            self.topology_file = argv[1]
        else:
            self.topology_file = input("Enter the filepath of the topology file: ")


    def get_nodes(self):
        import yaml 
        try:
            with open(self.topology_file, "r") as file:
                data = yaml.safe_load(file)
            nodes = data.get("topology",{}).get("nodes",{})
            return nodes

        except FileNotFoundError:
            print(f"Error: The topology '{self.topology_file}' was not found")
        except yaml.YAMLError as e:
            print(f"Error parsing YAML: {e}")
