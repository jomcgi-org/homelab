# Endpoint that reads Argo applications.
def handler(client):
    client.list("argoproj.io", "applications")  # needs verb: list
    return client.get("argoproj.io", "applications", name="x")  # needs verb: get
