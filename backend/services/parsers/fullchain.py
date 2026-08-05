"""全链条 - 在线表格解析器"""
from .base import BaseParser


class FullChainParser(BaseParser):
    parser_type = "全链条"
    table_name = "t_fullchain"
    COLUMNS = [
        "下发日期", "截止日期", "核查人", "社区", "来源",
        "姓名", "身份证号", "电话号码", "地址", "登记情况",
        "创建时间", "现住址", "核查结果", "研判", "二次反馈",
    ]
    # 当前腾讯表把“登记情况”作为正式业务列保存。旧版没有该列的 14 列表
    # 仍保留为兼容布局，读取后由 BaseParser 为登记情况补空。
    SOURCE_COLUMN_LAYOUTS = (
        tuple(COLUMNS),
        (
            "下发日期", "截止日期", "核查人", "社区", "来源",
            "姓名", "身份证号", "电话号码", "地址", "创建时间",
            "现住址", "核查结果", "研判", "二次反馈",
        ),
    )

    def get_business_key(self) -> list[str]:
        return ["身份证号", "电话号码", "下发日期"]
