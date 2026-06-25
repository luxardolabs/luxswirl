import json
import re


def extract_hostnames(raw_text):
    hostnames = []
    for line in raw_text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 3:
            name = parts[2]
            if name.lower() != "n/a" and not re.match(r"\d+\.\d+\.\d+\.\d+", name):
                hostnames.append(name)
    return hostnames


def generate_ping_tasks(hostnames, domain="example.com"):
    tasks = []
    for name in hostnames:
        fqdn = f"{name}.{domain}"
        tasks.append(
            {
                "name": f"ping_{name}",
                "type": "ping",
                "target": fqdn,
                "interval": 15,
                "retries": 2,
                "timeout": 2,
            }
        )
    return tasks


if __name__ == "__main__":
    with open("input.txt") as f:
        raw_data = f.read()

    hostnames = extract_hostnames(raw_data)
    ping_tasks = generate_ping_tasks(hostnames)

    print(json.dumps(ping_tasks, indent=4))
