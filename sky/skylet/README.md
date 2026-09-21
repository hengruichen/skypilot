# Skylet

Skylet is a lightweight, portable, and flexible Python framework for writing
cloud-native applications. It provides a simple and intuitive API for
orchestrating cloud resources, managing containers, and executing tasks on
distributed systems.

## Key Features

- **Cloud-Native Design**: Built for modern cloud environments, supporting
  multiple cloud providers and flexible resource management.
- **Container Orchestration**: Manage and execute tasks within Docker
  containers, leveraging Sky's container registry and image management.
- **Flexible Resource Management**: Define and manage resources dynamically,
  supporting various resource types and configurations.
- **Task Execution**: Execute tasks on distributed systems, with support for
  parallel and sequential execution.
- **Modular Architecture**: Designed with a modular architecture, allowing for
  easy integration and customization.

## Installation

To install Skylet, use pip:

```bash
pip install sky
```

## Quick Start

Here's a simple example of how to use Skylet to run a task on a remote
instance:

```python
from sky.skylet import app
from sky.skylet import resources
from sky.skylet import task

# Define the task
@task
def hello_world():
    print("Hello, World!")

# Define the app
@task
def main():
    hello_world()

# Create the app
app = app.App(
    name="my-app",
    resources=resources.Resources(
        cloud=resources.Cloud.AWS(),
        instance_type="p3.2xlarge",
    ),
    tasks=[main],
)

# Run the app
app.run()
```

## Documentation

For more detailed information, please refer to the [Skylet Documentation](https://docs.sky.com/skylet/).

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the Apache License 2.0 - see the LICENSE file
for details.
