"""快速检查 BPIC 2020 DomesticDeclarations 数据概况。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_loader import load_xes, summarize


def main():
    xes = Path("/sessions/stoic-adoring-faraday/mnt/datamind/data/DomesticDeclarations.xes")
    print(f"加载: {xes.name} ({xes.stat().st_size / 1024 / 1024:.1f} MB)")
    df = load_xes(xes)
    s = summarize(df)
    print("\n=== 数据概况 ===")
    for k, v in s.items():
        if k != "columns":
            print(f"  {k}: {v}")
    print(f"\n前 10 个活动:")
    if "concept:name" in df.columns:
        print(df["concept:name"].value_counts().head(10).to_string())
    print(f"\n资源 Top 10:")
    if "org:resource" in df.columns:
        print(df["org:resource"].value_counts().head(10).to_string())
    print(f"\nLifecycle 分布:")
    if "lifecycle:transition" in df.columns:
        print(df["lifecycle:transition"].value_counts().to_string())


if __name__ == "__main__":
    main()