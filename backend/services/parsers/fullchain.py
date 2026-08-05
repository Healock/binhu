"""全链条 - 在线表格解析器"""
from .base import BaseParser


class FullChainParser(BaseParser):
    parser_type = "全链条"
    table_name = "t_fullchain"
    COLUMNS = [
        "下发日期", "截止日期", "核查人", "社区", "来源",
        "姓名", "身份证号", "电话号码", "地址", "创建时间",
        "现住址", "核查结果", "研判", "二次反馈",
    ]
    # 当前腾讯表在“地址”和“创建时间”之间增加了“登记情况”。该字段不进入
    # 业务库和统计，但必须占据物理列位置，否则后续五列会整体错位。旧版没有
    # 该列的 14 列表仍保留为第二种兼容布局。
    SOURCE_COLUMN_LAYOUTS = (
        (
            "下发日期", "截止日期", "核查人", "社区", "来源",
            "姓名", "身份证号", "电话号码", "地址", "登记情况",
            "创建时间", "现住址", "核查结果", "研判", "二次反馈",
        ),
        tuple(COLUMNS),
    )

    def get_business_key(self) -> list[str]:
        return ["身份证号", "电话号码", "下发日期"]
