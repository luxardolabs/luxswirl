import json


def generate_hostnames(start, end, domain):
    hosts = []
    for i in range(start, end + 1):
        padded = str(i).zfill(2)
        hosts.append(f"esp{padded}.{domain}")
    return hosts


def generate_ping_tasks(hosts):
    tasks = []
    for host in hosts:
        name = host.split(".")[0]  # just 'esp01', 'esp02', etc.
        tasks.append(
            {
                "name": f"ping_{name}.example.com",
                "type": "ping",
                "target": host,
                "interval": 15,
                "retries": 2,
                "timeout": 2,
            }
        )
    return tasks


if __name__ == "__main__":
    domain = "example.com"
    start = 1
    end = 34

    hosts = generate_hostnames(start, end, domain)
    ping_tasks = generate_ping_tasks(hosts)

    print(json.dumps(ping_tasks, indent=4))
