"""Cloud provider factory."""


def make_cvm(cloud_cfg: dict):
    provider = str(cloud_cfg.get("provider", "tencent")).lower()
    if provider == "aliyun":
        from .aliyun import AliyunECS

        return AliyunECS(cloud_cfg["aliyun"])
    if provider == "tencent":
        from .tencent import TencentCVM

        return TencentCVM(cloud_cfg["tencent"])
    raise ValueError(f"未知云厂商: {provider}")

