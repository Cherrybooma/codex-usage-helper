"""Keep a pre-existing Markdown projection identity during source consolidation.

Local scanner/SQLite identities are never rewritten. An explicitly verified
alias changes only the namespace of the exported projection, so taking over a
vault from another runtime updates its existing notes instead of duplicating
the same log source under a second dataset ID.
"""
from copy import deepcopy
import re

from markdown_store import MarkdownError, ID

DATASET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,199}$")


def publication_payloads(config, saved, accounting):
    aliases = config.get("publication_dataset_aliases", {})
    if not isinstance(aliases, dict) or any(
        not isinstance(k, str) or not DATASET.fullmatch(k)
        or not isinstance(v, str) or not ID.fullmatch(v)
        for k, v in aliases.items()
    ):
        raise MarkdownError("invalid_alias", "账本发布身份映射无效，请保留原账本并检查配置。")
    if any(value in aliases and aliases[value] != value for value in aliases.values()):
        raise MarkdownError("invalid_alias", "发布身份不能使用链式映射。")
    saved, accounting = deepcopy(saved), deepcopy(accounting)
    datasets = {row.get("dataset_id") for row in saved.get("rows", [])}
    if saved.get("dataset_id"):
        datasets.add(saved["dataset_id"])
    if accounting.get("dataset_id"):
        datasets.add(accounting["dataset_id"])
    mapped = [aliases.get(value, value) for value in datasets]
    if len(set(mapped)) != len(mapped):
        raise MarkdownError("ambiguous_alias", "同批投入包含相互重叠的来源，未发布。")
    for payload in (saved, accounting):
        if payload.get("dataset_id") in aliases:
            payload["dataset_id"] = aliases[payload["dataset_id"]]
    for row in saved.get("rows", []):
        if row.get("dataset_id") in aliases:
            row["dataset_id"] = aliases[row["dataset_id"]]
    return saved, accounting
