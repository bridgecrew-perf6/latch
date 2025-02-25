import json
import os
import select
import sys
import textwrap
from pathlib import Path

import kubernetes
import requests
import websocket
from kubernetes.client import Configuration
from kubernetes.client.api import core_v1_api
from kubernetes.stream import stream

from latch_cli.config.latch import LatchConfig
from latch_cli.utils import account_id_from_token, retrieve_or_login

config = LatchConfig()
endpoints = config.sdk_endpoints


def _construct_kubeconfig(
    cert_auth_data: str,
    cluster_endpoint: str,
    account_id: str,
    access_key: str,
    secret_key: str,
    session_token: str,
) -> str:

    open_brack = "{"
    close_brack = "}"
    region_code = "us-west-2"
    cluster_name = "prion-prod"

    return textwrap.dedent(
        f"""apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: {cert_auth_data}
    server: {cluster_endpoint}
  name: arn:aws:eks:{region_code}:{account_id}:cluster/{cluster_name}
contexts:
- context:
    cluster: arn:aws:eks:{region_code}:{account_id}:cluster/{cluster_name}
    user: arn:aws:eks:{region_code}:{account_id}:cluster/{cluster_name}
  name: arn:aws:eks:{region_code}:{account_id}:cluster/{cluster_name}
current-context: arn:aws:eks:{region_code}:{account_id}:cluster/{cluster_name}
kind: Config
preferences: {open_brack}{close_brack}
users:
- name: arn:aws:eks:{region_code}:{account_id}:cluster/{cluster_name}
  user:
    exec:
      apiVersion: client.authentication.k8s.io/v1beta1
      command: aws
      args:
        - --region
        - {region_code}
        - eks
        - get-token
        - --cluster-name
        - {cluster_name}
      env:
        - name: 'AWS_ACCESS_KEY_ID'
          value: '{access_key}'
        - name: 'AWS_SECRET_ACCESS_KEY'
          value: '{secret_key}'
        - name: 'AWS_SESSION_TOKEN'
          value: '{session_token}'"""
    )


def _fetch_pod_info(token: str, task_name: str) -> (str, str, str):

    headers = {"Authorization": f"Bearer {token}"}
    data = {"task_name": task_name}

    response = requests.post(endpoints["pod-exec-info"], headers=headers, json=data)

    try:
        response = response.json()
        access_key = response["tmp_access_key"]
        secret_key = response["tmp_secret_key"]
        session_token = response["tmp_session_token"]
        cert_auth_data = response["cert_auth_data"]
        cluster_endpoint = response["cluster_endpoint"]
        namespace = response["namespace"]
        aws_account_id = response["aws_account_id"]
    except KeyError as err:
        raise ValueError(f"malformed response on image upload: {response}") from err

    return (
        access_key,
        secret_key,
        session_token,
        cert_auth_data,
        cluster_endpoint,
        namespace,
        aws_account_id,
    )


def execute(task_name: str):

    token = retrieve_or_login()
    (
        access_key,
        secret_key,
        session_token,
        cert_auth_data,
        cluster_endpoint,
        namespace,
        aws_account_id,
    ) = _fetch_pod_info(token, task_name)

    account_id = account_id_from_token(token)
    if int(account_id) < 10:
        account_id = f"x{account_id}"

    config_data = _construct_kubeconfig(
        cert_auth_data,
        cluster_endpoint,
        aws_account_id,
        access_key,
        secret_key,
        session_token,
    )
    config_file = Path("config").resolve()

    with open(config_file, "w") as c:
        c.write(config_data)

    kubernetes.config.load_kube_config("config")

    core_v1 = core_v1_api.CoreV1Api()

    # TODO
    pod_name = task_name

    wssock = stream(
        core_v1.connect_get_namespaced_pod_exec,
        pod_name,
        namespace,
        command=["/bin/sh"],
        stderr=True,
        stdin=True,
        stdout=True,
        tty=True,
        _preload_content=False,
    ).sock

    stdin_channel = bytes([kubernetes.stream.ws_client.STDIN_CHANNEL])
    stdout_channel = kubernetes.stream.ws_client.STDOUT_CHANNEL
    stderr_channel = kubernetes.stream.ws_client.STDERR_CHANNEL

    stdin = sys.stdin.fileno()
    stdout = sys.stdout.fileno()
    stderr = sys.stderr.fileno()

    rlist = [wssock.sock, stdin]

    while True:

        rs, _ws, _xs = select.select(rlist, [], [])

        if stdin in rs:
            data = os.read(stdin, 32 * 1024)
            if len(data) > 0:
                wssock.send(stdin_channel + data, websocket.ABNF.OPCODE_BINARY)

        if wssock.sock in rs:
            opcode, frame = wssock.recv_data_frame(True)
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                rlist.remove(wssock.sock)
            elif opcode == websocket.ABNF.OPCODE_BINARY:
                channel = frame.data[0]
                data = frame.data[1:]
                if channel in (stdout_channel, stderr_channel):
                    if len(data):
                        if channel == stdout_channel:
                            os.write(stdout, data)
                        else:
                            os.write(stderr, data)
                elif channel == kubernetes.stream.ws_client.ERROR_CHANNEL:
                    wssock.close()
                    error = json.loads(data)
                    if error["status"] == "Success":
                        return 0
                    if error["reason"] == "NonZeroExitCode":
                        for cause in error["details"]["causes"]:
                            if cause["reason"] == "ExitCode":
                                return int(cause["message"])
                    print(file=sys.stderr)
                    print(
                        f"Status: {error['status']} - Message: {error['message']}",
                        file=sys.stderr,
                    )
                    print(file=sys.stderr, flush=True)
                    sys.exit(1)
                else:
                    print(file=sys.stderr)
                    print(f"Unexpected channel: {channel}", file=sys.stderr)
                    print(f"Data: {data}", file=sys.stderr)
                    print(file=sys.stderr, flush=True)
                    sys.exit(1)
            else:
                print(file=sys.stderr)
                print(f"Unexpected websocket opcode: {opcode}", file=sys.stderr)
                print(file=sys.stderr, flush=True)
                sys.exit(1)
