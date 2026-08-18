"""Build LBORA importable assets from the two source docx files.

Reads:
  LBORA_客服标准FAQ_V1.0.docx      -> qa_pairs_import.json (Q1-Q24, internal
                                      chargeback entries Q25/Q26 are excluded)
  LBORA_商品售后QA知识库_V1.0.docx  -> LBORA_客服知识库_纯净版_V1.0.docx
                                      (customer-safe policy/decision knowledge,
                                      templates and internal rules stripped)

Run from the repo root:
  python3 backend/scripts/build_lbora_assets.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import docx
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH

REPO_ROOT = Path(__file__).resolve().parents[2]

# FAQ Q number -> admin category (Q25/Q26 are internal-only, intentionally absent).
CATEGORY_BY_Q = {
    range(1, 5): "售前与政策",
    range(5, 8): "尺码与适配",
    range(8, 13): "退货与退款",
    range(13, 17): "质量与商品状态",
    range(17, 20): "发错货与换货",
    range(20, 24): "物流",
    range(24, 25): "物流保险",
}

_Q_RE = re.compile(r"^Q(\d+)\.\s+(.*)$")
_SECTION_RE = re.compile(r"^[一二三四五六七八九十]+、(.+)$")


def _category_for(q_num: int) -> str | None:
    for rng, cat in CATEGORY_BY_Q.items():
        if q_num in rng:
            return cat
    return None  # Q25/Q26 and beyond: internal, skipped.


def parse_faq(doc_path: Path) -> list[dict]:
    """Extract Q1-Q24 as {question, answer, category} in English."""
    doc = docx.Document(str(doc_path))
    entries: list[dict] = []
    pending: dict | None = None

    def finalize() -> None:
        nonlocal pending
        if pending is not None:
            answers = [a for a in pending["answers"] if a]
            if answers:
                cat = _category_for(pending["num"])
                if cat is not None:  # skip internal Q25/Q26
                    entries.append(
                        {
                            "question": pending["q"],
                            "answer": "\n\n".join(answers).strip(),
                            "category": cat,
                        }
                    )
            pending = None

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        if _SECTION_RE.match(text):
            finalize()
            continue
        m = _Q_RE.match(text)
        if m:
            finalize()
            q_num = int(m.group(1))
            # English question = everything before the last " / " (Chinese part).
            parts = m.group(2).split(" / ")
            english = " / ".join(parts[:-1]).strip() if len(parts) > 1 else m.group(2).strip()
            pending = {"num": q_num, "q": english, "answers": []}
            continue
        if pending is not None:
            pending["answers"].append(text)
    finalize()
    return entries


# ---------------------------------------------------------------- clean KB docx

KB_SECTIONS: list[tuple[str, list[str]]] = [
    (
        "核心处理原则",
        [
            "先分类再回复：物流、尺码、主观不满意、质量缺陷、运输损坏、发错货、退货、拒付风险。",
            "客户首次提出退货：先了解原因；尺码/主观不满意/轻微问题可先给调节方法、部分退款或优惠方案。",
            "客户明确坚持退货、引用官网政策、抱怨退货困难，或提到 bank / credit card / chargeback / dispute / consumer complaint：停止反复挽留，按公开政策处理。",
            "没有照片证据时，不主动承认 defective / poor quality。主观评价统一使用：\"We're sorry the product didn't meet your expectations.\"",
            "Dirty / Defective / Broken / Wrong Item 等客观问题：先索取整体、问题部位、标签、外包装照片，再定责。",
            "物流正常运输中：提供最新物流节点与 17TRACK 链接，不退款；确认丢失、严重异常或无法派送后再补发/退款。",
            "所有承诺与官网公开政策一致。",
        ],
    ),
    (
        "现行退换货政策",
        [
            "30 天退货：收到订单后 30 天内可申请。",
            "退货商品须未穿戴、未使用、保持原状、原始标签齐全、原包装，并提供订单号/购买凭证。",
            "个人偏好类（改变主意、不喜欢、订错、尺码/适配不满意）：客户承担退货运费，退款扣 USD $20 Return Processing Fee。",
            "制造缺陷、运输损坏、LBORA 发错商品：不收 $20 处理费；核实后可补发或全额退款。",
            "$20 Return Processing Fee 覆盖检查、处理与上架成本，仅适用于个人偏好类退货。",
            "收到退货并检查后，退款原路返回；审核通过后通常 10 个工作日内处理（银行到账时间可能更长）。",
            "未经授权的退货不可直接接受；不主动向客户提供退货地址，须先审核通过。",
        ],
    ),
    (
        "退货仓地址（每次只向客户提供一个已授权地址，按地区选择）",
        [
            "美西洛杉矶仓：Mars Shipping Service LLC, 2965 E Vernon Ave, Vernon, CA 90058, United States, Phone +1 6264105865",
            "新泽西仓：Mars Shipping Service LLC, 1100 Randolph Rd. Ste B, Somerset, NJ 08873, United States, Phone +1 6467979999",
        ],
    ),
    (
        "售后判断矩阵",
        [
            "尺码不合适 → 确认可调节内围/头围 → 调节 + $20-30 部分退款 → 坚持退货则按政策",
            "不喜欢/觉得廉价 → 主观体验，不承认缺陷 → $20-30 部分退款 → 坚持退货则按政策",
            "轻微压痕 → 看照片/判断可恢复 → 恢复指导 + $15-25 → 严重则进入损坏流程",
            "Dirty/Defective/Broken → 索取照片/标签/包装 → 核实后补发/退款 → 严重损坏则全额解决",
            "发错货/尺码 → 核实标签和订单 → 免费补发或退款 → 拒绝补发则退款",
            "物流正常 → 查 17TRACK → 节点+链接+等待 → 长期无更新则调查",
            "派送失败 → 确认二次派送/退回状态 → 协助派送/调查 → 无法派送则补发/退款",
            "严重运输损坏 → 照片证据 → 免费补发或全额退款 → 补发无意义则退款",
            "拒付风险 → 停止拉扯 → 清晰退货/退款路径 → 已拒付则走支付渠道流程",
        ],
    ),
    (
        "标准谈判梯度",
        [
            "第1轮：解决问题：调节、恢复、物流解释或核实，不直接退款。",
            "第2轮：保留商品 + 部分退款：通常 $20-30。强调客户可省退货运费和等待时间。",
            "第3轮：按政策退货：客户仍坚持时，给地址、$20 Processing Fee、客户承担退货运费、追踪号要求。",
            "立即停止挽留：I just want to return / honor your return policy / hard to return / chargeback / dispute / bank / credit card / consumer complaint。",
        ],
    ),
    (
        "物流与派送知识",
        [
            "订单发货后通过 17TRACK 追踪：https://shoplbora.com/apps/17TRACK",
            "物流正常运输中：提供最新节点与链接，不退款。",
            "清关/到达机场后：进入目的地派送网络，转运至本地承运商；转运期间物流更新可能短暂暂停。",
            "Shopify 显示 \"waiting for details\" 可能是更新滞后于承运商系统：以承运商最新状态为准。",
            "派送失败：确认是否有二次派送、自提或退回流程；无法派送或确认丢失则补发/退款。",
            "客户需在特定日期/活动前收货：发货前说明无法保证具体送达日期，除非承运商明确保证。",
        ],
    ),
    (
        "拒付与投诉处理原则（对客口径）",
        [
            "客户提及 chargeback / dispute / bank / credit card / consumer complaint：停止反复挽留，不争论对错，提供与公开政策一致的明确可执行的退货/退款/换货路径。",
            "已发起拒付：避免重复赔付；先请客户通过银行/发卡行渠道处理，确认撤诉后再由商家退款，并保留书面确认与支付渠道记录。",
        ],
    ),
    (
        "客服语言规范（对客口径）",
        [
            "主观不满意统一使用：\"We're sorry the product didn't meet your expectations.\"",
            "未核实前不使用 \"Our product is defective / poor quality.\"",
            "挽留时强调客户利益：\"This option may save you return shipping costs and time.\"",
            "不使用商家视角措辞：\"Returning it will make us lose money / It is uneconomical for us.\"",
            "不承诺具体送达日期，除非承运商/服务明确保证。",
            "客户已坚持退货时，不连续多轮推荐新商品或优惠券。",
            "所有金额、库存、物流节点、退款状态必须核实后再写入回复。",
        ],
    ),
]

KB_HEADER = (
    "本文件为对客安全的知识库版本（已剔除内部拒付处理流程、内部判断口径与回复模板）。"
    "上传至售后智能体知识库后，回复生成时会全文注入作为事实依据，仅可引用本文件信息，禁止编造。"
)


def _add_heading(doc: docx.Document, text: str, level: int) -> None:
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = True
    run.font.size = docx.shared.Pt(16 if level == 0 else 13)
    p.space_after = docx.shared.Pt(4)


def _add_bullets(doc: docx.Document, items: list[str]) -> None:
    for item in items:
        p = doc.add_paragraph(item, style="List Bullet")
        p.paragraph_format.space_after = docx.shared.Pt(2)


def build_kb_docx(out_path: Path) -> None:
    doc = docx.Document()

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    tr = title.add_run("LBORA 客服知识库（纯净版）")
    tr.bold = True
    tr.font.size = docx.shared.Pt(20)

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sr = sub.add_run("Customer Service Knowledge Base · V1.0")
    sr.font.size = docx.shared.Pt(12)

    note = doc.add_paragraph()
    note.add_run(KB_HEADER).font.size = docx.shared.Pt(10)
    note.paragraph_format.space_after = docx.shared.Pt(8)

    for i, (heading, items) in enumerate(KB_SECTIONS, start=1):
        _add_heading(doc, f"{i}. {heading}", 1)
        _add_bullets(doc, items)

    doc.save(str(out_path))


# ----------------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(description="Build LBORA importable QA + clean KB assets")
    parser.add_argument("--faq", type=Path, default=REPO_ROOT / "LBORA_客服标准FAQ_V1.0.docx")
    parser.add_argument("--out-json", type=Path, default=REPO_ROOT / "qa_pairs_import.json")
    parser.add_argument("--out-docx", type=Path, default=REPO_ROOT / "LBORA_客服知识库_纯净版_V1.0.docx")
    args = parser.parse_args()

    if not args.faq.exists():
        raise SystemExit(f"source FAQ not found: {args.faq}")

    entries = parse_faq(args.faq)
    payload = {"items": entries}
    args.out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] QA entries: {len(entries)} -> {args.out_json}")

    build_kb_docx(args.out_docx)
    print(f"[ok] clean KB docx -> {args.out_docx}")


if __name__ == "__main__":
    main()
