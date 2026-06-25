import json
import sys


def generate_ping_tasks(hosts):
    tasks = []
    for host in hosts:
        tasks.append(
            {
                "name": f"ping_{host.replace('.', '_').replace(':', '_')}",
                "type": "ping",
                "target": host,
                "interval": 15,
                "retries": 2,
                "timeout": 2,
            }
        )
    return tasks


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python ping_task_generator.py host1 host2 host3 ...")
        sys.exit(1)

    host_list = sys.argv[1:]
    output = generate_ping_tasks(host_list)
    print(json.dumps(output, indent=4))
