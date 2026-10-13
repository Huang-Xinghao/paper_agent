"""Look up BatteryLife raw-data sources and annotate the local dataset catalog."""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from pathlib import Path


OFFICIAL_README = "https://raw.githubusercontent.com/Ruifeng-Tan/BatteryLife/main/README.md"
CATALOG = Path(__file__).resolve().parents[2] / "电池资源库" / "数据集"
SOURCE_LINES = {
    "CALB": "Zn-ion, Na-ion, and CALB datasets",
    "NA-ion": "Zn-ion, Na-ion, and CALB datasets",
    "ZN-coin": "Zn-ion, Na-ion, and CALB datasets",
    "CALCE": "CALCE",
    "MATR": "MATR",
    "HUST": "HUST",
    "RWTH": "RWTH",
    "ISU-ILCC": "ISU\\_ILCC",
    "XJTU": "XJTU",
    "Tongji": "Tongji",
    "Stanford": "Stanford",
    "Stanford_2": "Stanford",
    "HNEI": "HNEI, SNL, MICH, MICH_EXP and UL_PUR datasets",
    "SNL": "HNEI, SNL, MICH, MICH_EXP and UL_PUR datasets",
    "MICH": "HNEI, SNL, MICH, MICH_EXP and UL_PUR datasets",
    "MICH_EXP": "HNEI, SNL, MICH, MICH_EXP and UL_PUR datasets",
    "UL_PUR": "HNEI, SNL, MICH, MICH_EXP and UL_PUR datasets",
    "SDU": "SDU dataset",
}
DESCRIPTIONS = {
    "CALB": "27 只 58 Ah NMC/石墨方形锂电池，覆盖 0/25/35/45°C 和两种协议；0°C 组为加速老化测试。",
    "CALCE": "13 只 1.1 Ah LCO/石墨方形锂电池，在 25°C 下使用两种快充协议。",
    "HNEI": "14 只 2.8 Ah、18650 规格的 LCO/NMC 电池，在 25°C 下比较两种快充协议和电芯个体差异。",
    "HUST": "77 只 1.1 Ah LFP/石墨 18650 电池，在 30°C 下循环；原始简介对协议数的表述需进一步核验。",
    "ISU-ILCC": "240 只 0.25 Ah NMC/石墨聚合物电池，在 30°C 下覆盖 202 种充放电协议。",
    "MATR": "169 只 1.1 Ah LFP/石墨 18650 电池，在 30°C 下覆盖 81 种快充协议，包含多个实验批次。",
    "MICH_EXP": "12 只 5 Ah NMC111/石墨软包电池，覆盖 -5/25/45°C 和六种协议。",
    "MICH": "40 只 2.36 Ah NMC111/石墨软包电池，覆盖 25/45°C 和两种协议。",
    "NA-ion": "31 只 1 Ah 钠离子 18650 电池，原始简介列出 25°C 下的 12 种充放电协议。",
    "RWTH": "48 只 3 Ah NMC/碳负极 18650 电池，在 25°C 和相同循环协议下测试。",
    "SDU": "86 只 2.4 Ah NMC/石墨 18650 电池，具有不同化成条件，并在 25°C 下使用 16 种循环协议。",
    "SNL": "52 只 18650 电池，涵盖 LFP、NCA、NMC 化学体系及 15/25/35°C、23 种协议。",
    "Stanford_2": "181 只 NMC532/石墨电池，比较不同化成协议；BatteryLife 称这是包含两次发布数据的完整版本。",
    "Stanford": "41 只 NMC532/石墨电池，比较不同化成协议；与 Stanford_2 存在发布批次重叠。",
    "Tongji": "108 只 18650 电池，涵盖 NCA、NCM 和 NCM+NCA 三组，使用 25/35/45°C 等不同工况；原始记录是 Zenodo 6405084。IC2ML 代码仓库所给 DOI 10.5281/zenodo.6379165 跳转到同一记录，可据此对应论文数据。",
    "UL_PUR": "10 只 3.4 Ah、18650 规格的 LCO/NMC 电池，在约 23°C 下循环；原始简介对充电协议是否变化的表述有冲突。",
    "XJTU": "本地简介记录 23 只 2 Ah NMC/石墨 18650 电池、20°C 和两种工况；BatteryLife v12 宣布补充了完整版本，数量应按下载版本核验。",
    "ZN-coin": "锌/二氧化锰扣式电池；本地简介记录 95 只，额定容量以第 10 圈放电容量为基准，数量与目录其他字段存在冲突。",
}
MARKER = "## BatteryLife 数据集检索"


def _get_text(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "paper-agent/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def _raw_source_lines(readme: str) -> dict[str, str]:
    section = readme.split("### Raw dataset\n", 1)[1].split("\n## ", 1)[0]
    return {line[2:].split(":", 1)[0].strip(): line
            for line in section.splitlines() if line.startswith("- ") and ":" in line}


def _check_url(url: str, timeout: float) -> tuple[bool, str]:
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, method=method,
                                         headers={"User-Agent": "paper-agent/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                if status == 200:
                    return True, response.url
                if method == "HEAD" and status == 202:
                    continue
                return False, f"HTTP {status}"
        except (urllib.error.URLError, TimeoutError) as exc:
            if method == "GET":
                return False, str(exc)
    return False, "No successful HTTP response"


def search_batterylife_dataset(dataset_id: str, *, readme: str | None = None,
                               timeout: float = 15.0) -> dict:
    """Return the official raw-data entry and verified links for an exact local ID."""
    if dataset_id not in SOURCE_LINES:
        raise ValueError(f"Unknown BatteryLife dataset ID: {dataset_id}")
    lines = _raw_source_lines(readme if readme is not None else _get_text(OFFICIAL_README, timeout))
    source = lines.get(SOURCE_LINES[dataset_id])
    if not source:
        return {"dataset_id": dataset_id, "success": False,
                "reason": "Raw-data entry absent from official README", "links": []}
    urls = re.findall(r"\]\((https?://[^)]+)\)", source)
    links = [{"url": url, "reachable": ok, "resolved_url": resolved}
             for url in urls for ok, resolved in [_check_url(url, timeout)]]
    if dataset_id == "ISU-ILCC" and not any(link["reachable"] for link in links):
        api_url = "https://api.figshare.com/v2/articles/22582234"
        try:
            article = json.loads(_get_text(api_url, timeout))
            if (article.get("id") == 22582234
                    and "ISU-ILCC Battery Aging Dataset" in article.get("title", "")
                    and article.get("files")):
                links.append({"url": api_url, "reachable": True, "resolved_url": api_url})
        except (OSError, ValueError, TimeoutError):
            pass
    return {"dataset_id": dataset_id, "description": DESCRIPTIONS[dataset_id],
            "source": OFFICIAL_README, "local_readme": str(CATALOG / f"{dataset_id}_README.md"),
            "links": links, "success": bool(links) and any(link["reachable"] for link in links),
            "reason": "No raw-data link returned HTTP 200" if links and not any(
                link["reachable"] for link in links) else ""}


def sync_batterylife_descriptions(catalog: Path = CATALOG, *, timeout: float = 15.0,
                                  write: bool = True) -> list[dict]:
    """Check every local dataset and upsert a sourced summary into its README."""
    readme = _get_text(OFFICIAL_README, timeout)
    results = []
    for path in sorted(catalog.glob("*_README.md")):
        dataset_id = path.name.removesuffix("_README.md")
        try:
            result = search_batterylife_dataset(dataset_id, readme=readme, timeout=timeout)
        except (ValueError, OSError) as exc:
            result = {"dataset_id": dataset_id, "success": False, "reason": str(exc)}
        results.append(result)
        if "links" not in result or not write:
            continue
        urls = "\n".join(f"- {'原始数据入口' if link['reachable'] else '官方所列入口（访问未核实）'}："
                         f"[{link['resolved_url'] if link['reachable'] else link['url']}]"
                         f"({link['resolved_url'] if link['reachable'] else link['url']})"
                         for link in result["links"])
        note = ("- 说明：BatteryArchive 提供的是多个数据集共用的入口页；具体文件需在站内查找。\n"
                if dataset_id in {"HNEI", "SNL", "MICH", "MICH_EXP", "UL_PUR"} else "")
        block = (f"{MARKER}\n\n{result['description']}\n\n"
                 f"- 官方索引：[BatteryLife Raw dataset]({OFFICIAL_README})\n"
                 f"{urls}\n{note}")
        original = path.read_bytes().decode("utf-8")
        if MARKER in original:
            original = original.split(MARKER, 1)[0]
        updated = original.rstrip("\r\n") + "\n\n" + block.rstrip() + "\n"
        if updated != path.read_bytes().decode("utf-8"):
            path.write_bytes(updated.encode("utf-8"))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify BatteryLife raw links and annotate local READMEs")
    parser.add_argument("--check", action="store_true", help="Check without writing READMEs")
    args = parser.parse_args()
    results = sync_batterylife_descriptions(write=not args.check)
    for result in results:
        print(f"{'OK' if result['success'] else 'FAIL'} {result['dataset_id']}"
              + (f": {result.get('reason', '')}" if not result["success"] else ""))
    print(f"Success: {sum(r['success'] for r in results)}; "
          f"failed: {sum(not r['success'] for r in results)}")


if __name__ == "__main__":
    main()
