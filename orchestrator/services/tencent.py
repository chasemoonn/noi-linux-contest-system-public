"""Tencent Cloud CVM start/stop client."""
import time

from tencentcloud.common import credential
from tencentcloud.cvm.v20170312 import cvm_client, models


class TencentCVM:
    def __init__(self, cfg: dict):
        cred = credential.Credential(cfg["secret_id"], cfg["secret_key"])
        self.client = cvm_client.CvmClient(cred, cfg["region"])
        self.instance_id = cfg["instance_id"]

    def status(self) -> tuple[str, str]:
        req = models.DescribeInstancesRequest()
        req.InstanceIds = [self.instance_id]
        resp = self.client.DescribeInstances(req)
        if not resp.InstanceSet:
            raise RuntimeError(f"腾讯云实例不存在: {self.instance_id}")
        ins = resp.InstanceSet[0]
        ip = ins.PublicIpAddresses[0] if ins.PublicIpAddresses else ""
        return ins.InstanceState, ip

    def start(self):
        req = models.StartInstancesRequest()
        req.InstanceIds = [self.instance_id]
        return self.client.StartInstances(req)

    def stop(self):
        req = models.StopInstancesRequest()
        req.InstanceIds = [self.instance_id]
        req.StopType = "SOFT_FIRST"
        req.StoppedMode = "STOP_CHARGING"
        return self.client.StopInstances(req)

    def wait_running(self, timeout: int = 300) -> str:
        deadline = time.monotonic() + timeout
        last_state = "UNKNOWN"
        while time.monotonic() < deadline:
            last_state, ip = self.status()
            if last_state == "RUNNING" and ip:
                return ip
            time.sleep(5)
        raise TimeoutError(f"等待腾讯云实例开机超时，最后状态 {last_state}")

