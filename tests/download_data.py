"""
下载 BPIC 2020 数据集到 data/ 目录。

BPIC 2020 是公开数据集,但 4TU.ResearchData 需要手动确认下载条款,
因此本脚本只提供 URL 列表和用法说明。

推荐数据集(按审批相关性排序):

1. **DomesticDeclarations**(国内报销,~30MB 压缩)
   https://data.4tu.nl/articles/dataset/Domestic_declarations_and_request_for_payment_log/12726953

2. **TravelExpenses**(差旅报销,~50MB 压缩)
   https://data.4tu.nl/articles/dataset/Travel_Expenses/12676915

3. **InternationalDeclarations**(国际报销,~20MB)
   https://data.4tu.nl/articles/dataset/International_Declarations/12726958

4. **PrepaidTravelCost**(预支差旅费,~15MB)
   https://data.4tu.nl/articles/dataset/Prepaid_Travel_Cost/12676936

使用方法:
    # 1. 浏览器打开上述 URL
    # 2. 点击页面下载按钮(会要求同意条款)
    # 3. 下载 DomesticDeclarations.xes.gzip 放到 data/ 目录
    # 4. 运行:
    gunzip data/DomesticDeclarations.xes.gzip
    python -m src.main --dataset domestic_declarations

国内下载如果网络不通,可以尝试:
    - 在公司内网下载后拷贝
    - 使用 VPN
    - 通过 GitHub 镜像搜索 "BPIC 2020" 的非官方副本
"""
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

URLS = {
    "DomesticDeclarations.xes": (
        "https://data.4tu.nl/articles/dataset/Domestic_declarations_and_request_for_payment_log/12726953"
    ),
    "TravelExpenses.xes": (
        "https://data.4tu.nl/articles/dataset/Travel_Expenses/12676915"
    ),
    "InternationalDeclarations.xes": (
        "https://data.4tu.nl/articles/dataset/International_Declarations/12726958"
    ),
    "PrepaidTravelCost.xes": (
        "https://data.4tu.nl/articles/dataset/Prepaid_Travel_Cost/12676936"
    ),
}


def main():
    print("=" * 60)
    print("BPIC 2020 数据集下载指引")
    print("=" * 60)
    print(f"\n请将 XES 文件下载到: {DATA_DIR}\n")

    for filename, url in URLS.items():
        print(f"• {filename}")
        print(f"  {url}\n")

    print("下载完成后,文件可能是 .xes.gzip 格式,需要先解压:")
    print("  cd data && gunzip *.xes.gzip\n")
    print("然后运行:")
    print("  python -m src.main --dataset domestic_declarations\n")
    print("=" * 60)
    print("注意:4TU.ResearchData 要求手动接受数据使用条款,")
    print("自动化下载需要账号登录,故需人工下载。")
    print("=" * 60)


if __name__ == "__main__":
    main()