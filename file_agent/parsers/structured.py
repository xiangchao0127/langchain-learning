"""结构化 / 半结构化文件解析器：JSON、JSONL、CSV、YAML。"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

import yaml

from ..loaders import read_text
from .base import BaseParser, ParserError, normalize_records

_JSON_LIKE_SUFFIXES = {".json", ".jsonl", ".ndjson"}


class JsonParser(BaseParser):
    """解析 .json（数组 / 对象）与 .jsonl / .ndjson（每行一个 JSON）。"""

    name = "json"
    description = "JSON / JSON Lines"
    extensions = (".json", ".jsonl", ".ndjson")

    def parse(self, path: str | Path) -> list[dict[str, Any]]:
        path = Path(path)
        text = read_text(path)
        if not text.strip():
            return []

        if path.suffix.lower() in {".jsonl", ".ndjson"}:
            return self._parse_json_lines(text)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            # 容错：整体不是合法 JSON 时，尝试按行解析
            records = self._parse_json_lines(text)
            if records:
                return records
            raise ParserError(f"JSON 解析失败: {exc}") from exc
        return normalize_records(data)

    @staticmethod
    def _parse_json_lines(text: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip().rstrip(",")
            if not line or line in {"[", "]"}:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                # 行内可能混入日志前缀，尝试从第一个 { 开始截取
                brace = line.find("{")
                if brace == -1:
                    continue
                try:
                    item = json.loads(line[brace:])
                except json.JSONDecodeError:
                    continue
            records.extend(normalize_records(item))
        if not records:
            raise ParserError("未从文件中解析出任何 JSON 记录")
        return records

    def sniff(self, path: str | Path, sample: str | None = None) -> bool:
        sample = (sample or self.sample(path)).strip()
        if not sample:
            return False
        if sample[0] in "[{":
            try:
                json.loads(sample if sample[0] == "{" else sample.rstrip().rstrip(",") + "]")
                return True
            except json.JSONDecodeError:
                return sample[0] == "{"
        # JSONL：每行都以 { 开头
        lines = [line.strip() for line in sample.splitlines() if line.strip()][:5]
        return bool(lines) and all(line.startswith("{") for line in lines)


class CsvParser(BaseParser):
    """解析 CSV / TSV / PSV，自动嗅探分隔符。"""

    name = "csv"
    description = "CSV / TSV 分隔符文本"
    extensions = (".csv", ".tsv", ".psv")

    def __init__(self, delimiter: str | None = None) -> None:
        self.delimiter = delimiter

    def _resolve_delimiter(self, text: str, suffix: str) -> str:
        if self.delimiter:
            return self.delimiter
        if suffix == ".tsv":
            return "\t"
        if suffix == ".psv":
            return "|"
        try:
            return csv.Sniffer().sniff(text[:10000], delimiters=",;\t|").delimiter
        except csv.Error:
            return ","

    def parse(self, path: str | Path) -> list[dict[str, Any]]:
        path = Path(path)
        text = read_text(path)
        if not text.strip():
            return []

        delimiter = self._resolve_delimiter(text, path.suffix.lower())
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        records: list[dict[str, Any]] = []
        for row in reader:
            cleaned: dict[str, Any] = {}
            for key, value in row.items():
                if key is None:
                    continue
                name = str(key).strip().lstrip("\ufeff")
                if isinstance(value, str):
                    value = value.strip()
                cleaned[name] = value
            if cleaned:
                records.append(cleaned)
        return records

    def sniff(self, path: str | Path, sample: str | None = None) -> bool:
        sample = sample or self.sample(path)
        lines = [line for line in sample.splitlines() if line.strip()][:5]
        if len(lines) < 2:
            return False
        for delimiter in (",", "\t", ";", "|"):
            counts = [line.count(delimiter) for line in lines]
            if counts[0] > 0 and all(count == counts[0] for count in counts):
                return True
        return False


class YamlParser(BaseParser):
    """解析 YAML / YML。"""

    name = "yaml"
    description = "YAML 文档"
    extensions = (".yaml", ".yml")

    def parse(self, path: str | Path) -> list[dict[str, Any]]:
        text = read_text(path)
        if not text.strip():
            return []
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ParserError(f"YAML 解析失败: {exc}") from exc
        return normalize_records(data)

    def sniff(self, path: str | Path, sample: str | None = None) -> bool:
        sample = sample or self.sample(path)
        if not re.search(r"^[\w\-. ]+:(\s|$)", sample, re.MULTILINE):
            return False
        # 排除明显的 JSON
        return not sample.lstrip().startswith(("{", "["))


__all__ = ["JsonParser", "CsvParser", "YamlParser", "_JSON_LIKE_SUFFIXES"]
