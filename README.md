# pccz_list_documents

面向 `list_documents.csv` 的专项抓取项目。

目标是把 `文书详情链接` 清洗后逐条抓取，优先提取正文；如果详情页只有附件，则继续下载 PDF / Word / 文本附件并抽取文本，最后按一行一条持续写入分片 CSV。

## 特点

- 只处理 `list_documents.csv`
- 输入流式读取，不把全表加载进内存
- 输出分片 CSV，适合千万级长任务
- `requests` 做主通道
- 二阶段可选 Playwright 修复脚本，专门处理 `needs_playwright=1` 的疑难行
- 对扫描 PDF / 图片附件支持 OCR
- 对老 `.doc/.wps` 明确标记为需要外部转换器
- 对 `文书详情链接` 先去掉所有空白字符

## 安装

```bash
pip install -r requirements.txt
pip install -e .
```

如果后续要跑浏览器修复：

```bash
python -m playwright install chromium
```

## 先跑前 100 条

```bash
python -m pccz_list_documents.pipeline ^
  --input-csv "D:\application\illegal\list_documents.csv" ^
  --output-dir "D:\application\illegal\pccz_list_documents\data\output\sample100" ^
  --start-row 1 ^
  --limit 100 ^
  --detail-workers 4 ^
  --max-in-flight 16 ^
  --flush-every 1
```

输出目录里会生成：

- `documents.part001.csv`
- `needs_playwright.part001.csv`
- `summary.json`

## 推荐的大批量跑法

### 阶段 1：发现正文和附件链接

```bash
python -m pccz_list_documents.discover ^
  --input-csv "D:\application\illegal\list_documents.csv" ^
  --output-dir "D:\application\illegal\pccz_list_documents\data\output\discover_000001_050000" ^
  --start-row 1 ^
  --limit 50000 ^
  --detail-workers 4 ^
  --max-in-flight 16
```

### 阶段 2：全文抽取

```bash
python -m pccz_list_documents.extract_stage ^
  --input-path "D:\application\illegal\pccz_list_documents\data\output\discover_000001_050000" ^
  --output-dir "D:\application\illegal\pccz_list_documents\data\output\extract_000001_050000" ^
  --extract-workers 4 ^
  --max-in-flight 16 ^
  --playwright-on-needs
```

### 单机多分片启动器

```bash
python -m pccz_list_documents.launcher ^
  --input-csv "D:\application\illegal\list_documents.csv" ^
  --output-root "D:\application\illegal\pccz_list_documents\data\output\batch_local" ^
  --start-row 1 ^
  --limit 200000 ^
  --shard-size 50000 ^
  --parallel-shards 2 ^
  --detail-workers 4 ^
  --extract-workers 4 ^
  --playwright-on-needs
```

### 详情页压力测试

```bash
python -m pccz_list_documents.pressure_test ^
  --input-csv "D:\application\illegal\list_documents.csv" ^
  --output-dir "D:\application\illegal\pccz_list_documents\data\output\pressure_test" ^
  --start-row 1 ^
  --limit 100 ^
  --workers "2,4,8" ^
  --rounds 1
```

### 两台机器平分

机器 A：

```bash
python -m pccz_list_documents.launcher ^
  --input-csv "D:\application\illegal\list_documents.csv" ^
  --output-root "D:\application\illegal\pccz_list_documents\data\output\host_a" ^
  --start-row 1 ^
  --limit 5000000
```

机器 B：

```bash
python -m pccz_list_documents.launcher ^
  --input-csv "D:\application\illegal\list_documents.csv" ^
  --output-root "D:\application\illegal\pccz_list_documents\data\output\host_b" ^
  --start-row 5000001 ^
  --limit 5000000
```

## 固定网络出口

如果你有合规的固定出口代理或网关，可以传：

```bash
--http-proxy http://host:port
--https-proxy http://host:port
--playwright-proxy-server http://host:port
```

项目只支持“固定网络出口”的配置，不内置代理池轮换。

## 跑大批量时的建议

不要把 1000 万条一次性写进一个 CSV。建议按区间或分片跑：

```bash
python -m pccz_list_documents.pipeline ^
  --input-csv "D:\application\illegal\list_documents.csv" ^
  --output-dir "D:\application\illegal\pccz_list_documents\data\output\batch_000001_100000" ^
  --start-row 1 ^
  --limit 100000 ^
  --detail-workers 5 ^
  --max-in-flight 25 ^
  --rows-per-part 50000 ^
  --flush-every 20
```

然后并行开多个实例，按不重叠的 `start-row / limit` 分段跑。

## 为什么默认分片

如果最终文本平均 5KB，1000 万条就是 50GB 量级，还没算 CSV 转义和附件明细。单文件不可维护，也不利于断点恢复。

## 主流程

1. 逐行读取 `list_documents.csv`
2. 清洗 `文书详情链接` 中的空白字符
3. `requests` 抓详情页
4. 提正文、附件链接、challenge 标记、空模板标记
5. 如果只有附件，则下载附件并抽取文本
6. 把结果持续写入分片 CSV
7. 对 `needs_playwright=1` 的行额外写出 `needs_playwright.csv`

## 附件状态

- `ok`：直接抽取成功
- `ok_ocr`：直接抽不出文本，但 OCR 补出文本
- `http_404`：详情页给了附件地址，但下载地址已失效
- `ocr_needed`：PDF 抽不出字，OCR 也没补出来
- `image_ocr_needed`：图片附件需要 OCR，但未成功
- `legacy_word_needs_converter`：老 `.doc/.wps`，本机缺少外部转换器
- `zip_no_supported_members`：压缩包里没有当前支持的可抽取成员

## Playwright 二阶段修复

```bash
python -m pccz_list_documents.fetch_playwright ^
  --input-csv "D:\application\illegal\pccz_list_documents\data\output\sample100\needs_playwright.part001.csv" ^
  --output-csv "D:\application\illegal\pccz_list_documents\data\output\sample100\needs_playwright_repaired.csv" ^
  --wait-ms 5000
```

这个脚本只处理首轮判定需要浏览器复核的记录，不参与高并发主通道。
