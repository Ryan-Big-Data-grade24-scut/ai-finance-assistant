"""虚构人物、账户与商户素材。

全部为编造数据：姓名、号码、卡号均为占位符，卡号只以掩码形式出现，
不生成身份证号、真实手机号或完整卡号。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache


# ── 人物 ──────────────────────────────────────────────────────
PERSONA = {
    "name": "周予安",
    "alipay_account": "138****0000",
    "wechat_nickname": "周予安",
    "mobile_tail": "0000",
}

# ── 账户 ──────────────────────────────────────────────────────
# platform 决定这笔流水会出现在哪份平台导出里；credit 类账户有额度限制。
@dataclass
class Account:
    id: str
    name: str
    kind: str            # debit / credit / wallet / fund / broker
    platform: str
    opening: Decimal
    mask: str = ""
    limit: Decimal | None = None


ACCOUNTS: dict[str, Account] = {
    "salary": Account("salary", "招商银行储蓄卡", "debit", "cmb_debit", Decimal("45000.00"),
                      mask="6225 **** **** 0371"),
    "mortgage_card": Account("mortgage_card", "工商银行储蓄卡", "debit", "icbc_debit", Decimal("8000.00"),
                             mask="6222 **** **** 8810"),
    "living_card": Account("living_card", "建设银行储蓄卡", "debit", "ccb_debit", Decimal("6000.00"),
                           mask="6217 **** **** 5533"),
    "credit": Account("credit", "招商银行信用卡", "credit", "cmb_credit", Decimal("0.00"),
                      mask="4392 **** **** 6620", limit=Decimal("80000.00")),
    "credit2": Account("credit2", "广发银行信用卡", "credit", "cgb_credit", Decimal("0.00"),
                       mask="6226 **** **** 1188", limit=Decimal("30000.00")),
    # 钱包账户的 opening 是平台内所有子产品的合计，实际可用余额由 WALLET_PRODUCTS 拆分。
    "alipay": Account("alipay", "支付宝", "wallet", "alipay", Decimal("47000.00")),
    "wechat": Account("wechat", "微信零钱", "wallet", "wechat", Decimal("28800.00")),
    "wallet_family": Account("wallet_family", "小荷包·家庭开销", "wallet", "alipay_wallet", Decimal("3000.00")),
    "wallet_travel": Account("wallet_travel", "小荷包·旅行基金", "wallet", "alipay_wallet", Decimal("1200.00")),
    "wallet_education": Account("wallet_education", "小荷包·教育金", "wallet", "alipay_wallet", Decimal("0.00")),
    "wallet_auto": Account("wallet_auto", "小荷包·自动攒", "wallet", "alipay_wallet", Decimal("0.00")),
    "fund": Account("fund", "天天基金", "fund", "fund", Decimal("86000.00")),
    "broker": Account("broker", "华泰证券", "broker", "broker", Decimal("54000.00")),
    "wealth": Account("wealth", "招商银行理财", "fund", "wealth", Decimal("120000.00")),
}

CREDIT_ACCOUNTS = ("credit", "credit2")
# 信用卡账单周期：账单日 / 还款日（每月固定日期）。
CREDIT_CYCLE = {
    "credit": (5, 23),
    "credit2": (10, 28),
}
CREDIT_LABEL = {
    "credit": "招商银行信用卡(6620)",
    "credit2": "广发银行信用卡(1188)",
}


# ── 平台内部资金产品 ──────────────────────────────────────────
# 支付宝：余额、余额宝、理财；微信：零钱、零钱通、理财通。
# payable 表示能不能直接付款；annual_yield 是七日年化的近似值。
@dataclass(frozen=True)
class WalletProduct:
    account: str
    name: str
    opening: Decimal
    payable: bool
    annual_yield: Decimal


WALLET_PRODUCTS: dict[str, WalletProduct] = {
    "alipay:余额": WalletProduct("alipay", "余额", Decimal("2800.00"), True, Decimal("0")),
    "alipay:余额宝": WalletProduct("alipay", "余额宝", Decimal("26000.00"), True,
                                   Decimal("0.0135")),
    "alipay:理财": WalletProduct("alipay", "理财", Decimal("18000.00"), False,
                                 Decimal("0.0280")),
    "wechat:零钱": WalletProduct("wechat", "零钱", Decimal("1800.00"), True, Decimal("0")),
    "wechat:零钱通": WalletProduct("wechat", "零钱通", Decimal("12000.00"), True,
                                   Decimal("0.0145")),
    "wechat:理财通": WalletProduct("wechat", "理财通", Decimal("15000.00"), False,
                                   Decimal("0.0260")),
}

DEFAULT_FUNDING = {"alipay": "余额", "wechat": "零钱"}
PAYABLE_FUNDING = {
    "alipay": ("余额", "余额宝"),
    "wechat": ("零钱", "零钱通"),
}

PAYMENT_FLOOR = {"余额宝": Decimal("15000.00"), "零钱通": Decimal("5000.00")}

HUABEI_LIMIT = Decimal("20000.00")       # 花呗额度，和信用卡额度分开
QINQING_LIMIT = Decimal("1500.00")       # 支付宝亲情卡每月额度
QINSHU_LIMIT = Decimal("1000.00")        # 微信亲属卡每月额度

# 余额宝的收益在账单里记在南方薪金宝这个货币基金名下。
YUEBAO_FUND = "南方薪金宝A"
YUEBAO_COMPANY = "南方基金管理股份有限公司"
ALIPAY_WEALTH = "建信理财"
WECHAT_WEALTH = "理财通"


# ── 收入与固定支出 ────────────────────────────────────────────
SALARY = Decimal("28000.00")          # 税后月薪
BONUS_MONTHS = (1,)                   # 1 月发年终奖
BONUS = SALARY * 3                    # 年终奖 = 3 个月工资
RAISE_MONTH = 4                       # 每年 4 月调薪
RAISE_RATE = Decimal("1.05")

MORTGAGE = Decimal("7800.00")         # 房贷（15 日从房贷卡扣）
CAR_LOAN = Decimal("2900.00")         # 车贷（8 日）
CONSUMER_LOAN_MONTHLY = Decimal("1280.00")
CREDIT_LOAN_MONTHLY = Decimal("800.00")
CREDIT_INSTALLMENT_MONTHLY = Decimal("1200.00")

PROPERTY_FEE = Decimal("1850.00")     # 每季度（1/4/7/10 月 5 日）
PARKING_FEE = Decimal("1800.00")      # 每季度（1/4/7/10 月 5 日）
TUITION = Decimal("8000.00")          # 每季度（1/4/7/10 月 10 日）
SPOUSE_TRANSFER = Decimal("6000.00")  # 配偶每月转入小荷包
LIVING_CARD_TOPUP = Decimal("1500.00")  # 每月转入生活缴费卡
UTILITY_TOTAL = Decimal("1000.00")    # 每月水电气/宽带/话费，按明细拆分

# 年缴保险：(月份, 日期, 险种, 金额, 交易对方)
INSURANCE_POLICIES = (
    (3, 18, "重疾险年缴保费", Decimal("12000.00"), "长宁人寿保险"),
    (6, 18, "百万医疗险保费", Decimal("1500.00"), "众安在线财险"),
    (7, 18, "家庭财产险保费", Decimal("300.00"), "平安产险"),
    (9, 18, "定期寿险保费", Decimal("3000.00"), "长宁人寿保险"),
    (11, 18, "车辆保险保费", Decimal("5500.00"), "平安产险"),
)

FUND_SIP = Decimal("5000.00")         # 每月定投
BROKER_TRADE_MIN = Decimal("3000.00")
BROKER_TRADE_MAX = Decimal("15000.00")

# 小荷包每月从工资卡定额转入：(钱包, 金额)
WALLET_ALLOWANCE = {
    "wallet_family": Decimal("500.00"),
    "wallet_education": Decimal("500.00"),
    "wallet_auto": Decimal("500.00"),
}

# 账户低水位：低于此值时从工资卡补钱，补款本身也是一笔真实转账。
# 月度定额转入之外只做兜底，避免同一周重复补款。
WALLET_FLOOR = {
    "alipay": Decimal("3000.00"),
    "wechat": Decimal("1000.00"),
    "wallet_family": Decimal("1000.00"),
    "wallet_travel": Decimal("300.00"),
    "wallet_education": Decimal("0.00"),
}
WALLET_TOPUP = {
    "alipay": Decimal("4000.00"),
    "wechat": Decimal("2000.00"),
    "wallet_family": Decimal("2000.00"),
    "wallet_travel": Decimal("500.00"),
    "wallet_education": Decimal("1000.00"),
}


# ── 商户池（全部虚构或平台名）────────────────────────────────
BREAKFAST = ("巷口包子铺", "老陈早点", "晨光豆浆油条", "米记肠粉", "青禾便利店", "每日鲜便利店")
BREAKFAST_ITEMS = ("鲜肉包 x2", "豆浆+油条", "小米粥+茶叶蛋", "煎饼果子", "肠粉+豆浆", "三明治+牛奶")

LUNCH = ("川味小馆", "禾下食堂", "面和居", "南城快餐", "湘里人家", "麦香简餐")
LUNCH_ITEMS = ("商务套餐", "牛肉面", "番茄鸡蛋盖饭", "宫保鸡丁饭", "兰州拉面", "两荤一素")

COFFEE = ("青柠咖啡", "拾光咖啡", "叁号咖啡")
COFFEE_ITEMS = ("生椰拿铁", "美式咖啡", "燕麦拿铁", "拿铁大杯")

DINNER = ("江湖菜馆", "米香小馆", "牛气火锅", "香锅人家", "江南小厨", "老碗面")
DINNER_ITEMS = ("两人晚餐", "酸菜鱼", "家常小炒", "麻辣香锅", "小火锅", "烤肉拼盘")

GROCERY = ("惠民生鲜", "家家乐超市", "美好生活超市", "邻里生鲜")
GROCERY_ITEMS = ("蔬菜水果", "日用品", "零食饮料", "粮油副食", "冷冻食品", "牛奶面包")

DINING_OUT = ("南湖酒楼", "聚味轩", "小满餐厅", "渔家小馆")
DINING_OUT_ITEMS = ("朋友聚餐", "家庭聚餐", "同事聚会", "生日宴")

TRANSPORT = ("手机地铁乘车码", "公交乘车码", "滴滴出行", "哈啰单车")
TRANSPORT_ITEMS = ("地铁乘车", "公交通勤", "快车-科技园", "共享单车")

MEDICAL = ("仁和药房", "康宁诊所", "市口腔医院")
MEDICAL_ITEMS = ("感冒药", "门诊挂号", "洗牙", "常规体检")

HAIRCUT = ("简理发", "青丝造型")
HAIRCUT_ITEMS = ("男士剪发", "洗剪吹")

SUBSCRIPTIONS = (
    ("云视听会员", "视频会员月费", Decimal("25.00")),
    ("听歌会员", "音乐会员月费", Decimal("15.00")),
    ("网盘会员", "云盘会员月费", Decimal("10.00")),
    ("健身体验卡", "健身房月卡", Decimal("199.00")),
)

TRAVEL_CITIES = ("杭州", "成都", "厦门", "西安", "大理")

SHOP_ONLINE = ("天猫", "淘宝", "京东")

TAOBAO_STORES = ("优衣库官方旗舰店", "小米官方旗舰店", "三只松鼠旗舰店",
                 "维达官方旗舰店", "晨光文具旗舰店")
TMALL_STORES = ("天猫超市", "海尔官方旗舰店", "耐克官方旗舰店", "欧莱雅官方旗舰店")
JD_STORES = ("京东自营", "京东超市", "美的京东自营旗舰店")

# 商品与店铺要匹配：在优衣库买「图书」一眼就是假数据。
# 值 = (平台交易分类, 可能的店铺)。
SHOP_CATALOG = {
    "taobao": {
        "日用百货": ("日用百货", ("维达官方旗舰店", "晨光文具旗舰店")),
        "数码配件": ("数码电器", ("小米官方旗舰店",)),
        "家居用品": ("居家生活", ("维达官方旗舰店", "晨光文具旗舰店")),
        "厨房小电": ("数码电器", ("小米官方旗舰店",)),
        "零食大礼包": ("日用百货", ("三只松鼠旗舰店",)),
        "图书": ("文化休闲", ("当当网官方旗舰店", "新华书店旗舰店")),
    },
    "tmall": {
        "日用百货": ("日用百货", ("天猫超市", "维达官方旗舰店")),
        "数码配件": ("数码电器", ("小米官方旗舰店",)),
        "家居用品": ("居家生活", ("海尔官方旗舰店",)),
        "厨房小电": ("数码电器", ("海尔官方旗舰店", "美的官方旗舰店")),
        "零食大礼包": ("日用百货", ("天猫超市",)),
        "图书": ("文化休闲", ("当当网官方旗舰店",)),
    },
    "jd": {
        "日用百货": ("日用百货", ("京东超市",)),
        "数码配件": ("数码电器", ("京东自营",)),
        "家居用品": ("居家生活", ("京东自营",)),
        "厨房小电": ("数码电器", ("美的京东自营旗舰店",)),
        "零食大礼包": ("日用百货", ("京东超市",)),
        "图书": ("文化休闲", ("京东图书自营",)),
    },
}
SHOP_ITEMS = tuple(SHOP_CATALOG["taobao"])

MEITUAN_STORES = ("塔斯汀汉堡", "张亮麻辣烫", "外婆家", "蜜雪冰城", "瑞幸咖啡")
MEITUAN_ITEMS = ("外卖订单", "到店套餐", "下午茶", "夜宵")

# 转账/红包的另一方：真实账单里是具体的人，不是一个模糊的「微信好友」。
WECHAT_FRIENDS = ("陈屿", "王磊", "许安然", "沈嘉树")
FAMILY = ("林知夏", "周母", "周小满")

FUNDS = (
    ("000001", "华禾成长混合"),
    ("110022", "易方成长混合"),
    ("161725", "招商白酒指数"),
    ("012414", "广发新能源混合"),
)
# (代码, 名称, 基准价)：价格量级贴近真实 A 股，避免出现 10 元银行股卖 79 元这种失真。
BROKER_STOCKS = (
    ("600000", "浦发银行", Decimal("10.20")),
    ("000651", "格力电器", Decimal("42.50")),
    ("601318", "中国平安", Decimal("56.00")),
    ("300750", "宁德时代", Decimal("262.00")),
    ("002594", "比亚迪", Decimal("320.00")),
)

# ── 交易对方身份 ─────────────────────────────────────────────
# 真实账单里，同一个交易对方应该始终对应同一个（脱敏）账号；不同商户则不能
# 共用同一个占位符。支付宝「对方账号」多为脱敏邮箱/手机号，平台内部交易为 /。
@dataclass(frozen=True)
class Party:
    name: str
    alipay: str = "/"
    wechat: str = "/"
    bank: str = ""
    merchant_id: str = ""
    kind: str = "merchant"      # platform / merchant / person


# 明确列出平台、机构、家人和常用商户，保证账号形态合理且稳定。
PARTIES: dict[str, Party] = {
    # 平台内部
    "余额宝": Party("余额宝", "jij***@southernfund.com", "/", "", "", "platform"),
    "南方基金管理股份有限公司": Party("南方基金管理股份有限公司", "/", "/", "", "", "platform"),
    "南方薪金宝A": Party("南方薪金宝A", "/", "/", "", "", "platform"),
    "花呗": Party("花呗", "/", "/", "", "", "platform"),
    "支付宝红包": Party("支付宝红包", "/", "/", "", "", "platform"),
    "支付宝": Party("支付宝", "/", "/", "", "", "platform"),
    "微信零钱": Party("微信零钱", "/", "/", "", "", "platform"),
    "微信支付": Party("微信支付", "/", "/", "", "", "platform"),
    "理财通": Party("理财通", "/", "/", "", "", "platform"),
    "零钱通": Party("零钱通", "/", "/", "", "", "platform"),
    "淘宝": Party("淘宝", "/", "/", "", "TB", "platform"),
    "天猫": Party("天猫", "/", "/", "", "TM", "platform"),
    "京东": Party("京东", "/", "/", "", "JD", "platform"),
    "美团": Party("美团", "it_***@meituan.com", "/", "", "MT", "platform"),
    "手机地铁乘车码": Party("手机地铁乘车码", "/", "/", "", "", "platform"),
    "公交乘车码": Party("公交乘车码", "/", "/", "", "", "platform"),
    "中国工商银行": Party("中国工商银行", "/", "/", "", "", "platform"),
    "工商银行": Party("工商银行", "/", "/", "", "", "platform"),
    "招商银行信用卡": Party("招商银行信用卡", "/", "/", "", "", "platform"),
    "广发银行信用卡": Party("广发银行信用卡", "/", "/", "", "", "platform"),
    "招商银行储蓄卡": Party("招商银行储蓄卡", "/", "/", "", "", "platform"),
    "招商银行储蓄卡(0371)": Party("招商银行储蓄卡(0371)", "/", "/", "", "", "platform"),
    "工商银行储蓄卡": Party("工商银行储蓄卡", "/", "/", "", "", "platform"),
    "建设银行储蓄卡": Party("建设银行储蓄卡", "/", "/", "", "", "platform"),
    "中国建设银行": Party("中国建设银行", "/", "/", "", "", "platform"),
    "广东电网": Party("广东电网", "/", "/", "", "", "platform"),
    "广州自来水": Party("广州自来水", "/", "/", "", "", "platform"),
    "广州燃气": Party("广州燃气", "/", "/", "", "", "platform"),
    "中国电信": Party("中国电信", "/", "/", "", "", "platform"),
    "招商银行理财": Party("招商银行理财", "/", "/", "", "", "platform"),
    "天天基金": Party("天天基金", "/", "/", "", "", "platform"),
    "华泰证券": Party("华泰证券", "/", "/", "", "", "platform"),
    "建信理财": Party("建信理财", "/", "/", "", "", "platform"),
    # 机构/公司
    "启明科技有限公司": Party("启明科技有限公司", "hr_***@qmtech.com", "/",
                              "**** **** **** 6601", "QM001", "merchant"),
    "锦园物业": Party("锦园物业", "wy_***@jinyuanwuye.com", "/",
                      "**** **** **** 2108", "JY001", "merchant"),
    "育才实验学校": Party("育才实验学校", "cwc***@yucai.edu.cn", "/",
                          "**** **** **** 3306", "YC001", "merchant"),
    "长宁人寿保险": Party("长宁人寿保险", "bxj***@changninglife.com", "/",
                          "**** **** **** 8812", "CN001", "merchant"),
    "众安在线财险": Party("众安在线财险", "bx***@zhongan.com", "/",
                          "**** **** **** 5520", "ZA001", "merchant"),
    "平安产险": Party("平安产险", "cx***@pingan.com.cn", "/",
                      "**** **** **** 7719", "PA001", "merchant"),
    "广州鹏辉汽车服务": Party("广州鹏辉汽车服务", "svc***@penghui-auto.com", "/",
                              "**** **** **** 4402", "PH001", "merchant"),
    "国家税务总局": Party("国家税务总局", "/", "/", "", "", "platform"),
    "招商银行汽车金融": Party("招商银行汽车金融", "auto***@cmbchina.com", "/",
                              "", "CMB-AUTO", "merchant"),
    "招联消费金融": Party("招联消费金融", "loan***@zlconsumer.com", "/",
                          "", "ZL001", "merchant"),
    "中国铁路": Party("中国铁路", "tick***@12306.cn", "/", "", "RAIL", "merchant"),
    "国家电网": Party("国家电网", "955***@sgcc.com.cn", "/", "", "SGCC", "merchant"),
    "中国移动": Party("中国移动", "100***@chinamobile.com", "/", "", "CMCC", "merchant"),
    "滴滴出行": Party("滴滴出行", "did***@didichuxing.com", "/", "", "DIDI", "platform"),
    "哈啰单车": Party("哈啰单车", "hello***@hellobike.com", "/", "", "HELLO", "platform"),
    # 家人与朋友（虚构人物，号码全部脱敏）
    "林知夏": Party("林知夏", "137****6621", "wxid_***6621", "**** **** **** 6621",
                    "", "person"),
    "周母": Party("周母", "135****9082", "wxid_***9082", "**** **** **** 9082",
                  "", "person"),
    "周小满": Party("周小满", "/", "wxid_***1024", "", "", "person"),
    "陈屿": Party("陈屿", "136****4521", "wxid_***4521", "**** **** **** 4521",
                  "", "person"),
    "王磊": Party("王磊", "139****7183", "wxid_***7183", "**** **** **** 7183",
                  "", "person"),
    "许安然": Party("许安然", "158****6032", "wxid_***6032", "**** **** **** 6032",
                    "", "person"),
    "沈嘉树": Party("沈嘉树", "186****2274", "wxid_***2274", "**** **** **** 2274",
                    "", "person"),
}

_DOMAINS = ("qq.com", "163.com", "126.com", "sina.com", "foxmail.com")


@lru_cache(maxsize=None)
def party(name: str) -> Party:
    """给任意交易对方一个稳定的脱敏身份。

    同一个名字永远得到同一个账号；这是「能区分对方是谁」的前提。
    小商户按真实样本的常见形态分散到 /、脱敏邮箱、脱敏手机号三种。
    """
    if name in PARTIES:
        return PARTIES[name]
    if name.startswith(("支付宝小荷包", "小荷包")):
        return Party(name, "/", "/", "", "", "platform")

    h = int(hashlib.sha256(name.encode("utf-8")).hexdigest(), 16)
    domain = _DOMAINS[h % len(_DOMAINS)]
    bucket = h % 10
    if bucket < 3:
        alipay = "/"
    elif bucket < 7:
        alipay = f"{100 + h % 900}***@{domain}"
    else:
        alipay = f"1{3 + h % 7}{h // 7 % 10}******{h // 70 % 100:02d}"
    wechat = f"wxid_***{h % 10000:04d}" if bucket >= 4 else "/"
    bank = f"**** **** **** {h % 10000:04d}" if bucket >= 2 else ""
    return Party(name, alipay, wechat, bank,
                 f"M{h % 10 ** 10:010d}", "merchant")


def party_account(name: str) -> str:
    return party(name).alipay


def merchant_id(name: str) -> str:
    return party(name).merchant_id or f"M{int(hashlib.sha256(name.encode()).hexdigest()[:10], 16):010d}"


# ── 交易对方的真实身份 ────────────────────────────────────────
# 真实账单里，「个体户小商贩」和「公司/连锁商户」是两类完全不同的对手方。
# 平台分类器经常把前者一概标成「商户消费」，这正是要复现的系统性偏差。
INDIVIDUAL_VENDORS = frozenset({
    "巷口包子铺", "老陈早点", "晨光豆浆油条", "米记肠粉",
    "川味小馆", "禾下食堂", "面和居", "南城快餐", "湘里人家", "麦香简餐",
    "青柠咖啡", "拾光咖啡", "叁号咖啡",
    "江湖菜馆", "米香小馆", "牛气火锅", "香锅人家", "江南小厨", "老碗面",
    "惠民生鲜", "家家乐超市", "美好生活超市", "邻里生鲜",
    "南湖酒楼", "聚味轩", "小满餐厅", "渔家小馆",
    "仁和药房", "康宁诊所", "简理发", "青丝造型",
    "育才文具店", "校车服务", "少年宫",
})

# 名字里出现这些词，多半是公司、连锁店、机构；其余小店按个体户处理。
MERCHANT_KEYWORDS = (
    "官方", "旗舰店", "连锁", "超市", "便利店", "医院", "诊所", "学校",
    "银行", "保险", "电信", "电网", "自来水", "燃气", "铁路", "航空",
    "滴滴", "哈啰", "美团", "京东", "淘宝", "天猫", "瑞幸", "蜜雪",
    "星巴克", "麦当劳", "肯德基", "酒店", "景区", "影院", "物业",
)


def party_kind(name: str) -> str:
    """个体的稳定身份口径：platform / merchant / individual / person。"""
    known = PARTIES.get(name)
    if known:
        return known.kind
    if name in INDIVIDUAL_VENDORS:
        return "individual"
    if any(word in name for word in MERCHANT_KEYWORDS):
        return "merchant"
    # 账单里绝大多数无名小店都是个体户；默认按个体户处理，
    # 这样平台把它们标成「商户消费」的偏差才能被真实统计出来。
    return "individual"


# ── 平台分类器的系统性偏差 ────────────────────────────────────
# 真实平台不是按语义分类，而是按字面关键词打标。这里复现几种典型错误：
# 1) 个体户小商贩一律落进「商户交易/其他」这一档，真实品类消失；
# 2) 按名字字面意思分类（小米→粮油、三只松鼠→宠物）；
# 3) 找不到关键词就归「其他」。
# 目标：至少一半以上的消费笔数在平台分类下是错的。
LITERAL_TRAPS = (
    ("小米", "粮油副食"),
    ("三只松鼠", "宠物用品"),
    ("苹果", "水果生鲜"),
    ("松鼠", "宠物用品"),
)

LITERAL_KEYWORDS = (
    ("超市", "日用百货"), ("便利店", "日用百货"), ("生鲜", "日用百货"),
    ("百货", "日用百货"), ("文具", "日用百货"),
    ("咖啡", "餐饮美食"), ("茶", "餐饮美食"), ("餐", "餐饮美食"),
    ("饭", "餐饮美食"), ("面", "餐饮美食"), ("火锅", "餐饮美食"),
    ("烧烤", "餐饮美食"), ("小吃", "餐饮美食"), ("汉堡", "餐饮美食"),
    ("麻辣烫", "餐饮美食"), ("冰城", "餐饮美食"),
    ("药", "医疗健康"), ("诊所", "医疗健康"), ("医院", "医疗健康"),
    ("理发", "美容美发"), ("剪发", "美容美发"), ("造型", "美容美发"),
    ("学校", "教育培训"), ("教育", "教育培训"), ("少年宫", "教育培训"),
    ("保险", "保险"), ("物业", "房租物业"), ("房租", "房租物业"),
    ("电费", "充值缴费"), ("水费", "充值缴费"), ("燃气", "充值缴费"),
    ("宽带", "充值缴费"), ("话费", "充值缴费"),
    ("会员", "文化休闲"), ("影院", "文化休闲"), ("景区", "酒店旅游"),
    ("酒店", "酒店旅游"), ("铁路", "交通出行"), ("地铁", "交通出行"),
    ("公交", "交通出行"), ("单车", "交通出行"), ("滴滴", "交通出行"),
    ("旗舰店", "日用百货"), ("官方", "日用百货"),
)


def platform_category(true_category: str, *, direction: str, kind: str,
                      counterparty: str, counterparty_type: str,
                      item: str = "") -> str:
    """平台对外的「交易分类」——故意保留真实平台的粗粒度与字面偏差。"""
    if direction == "收入":
        return "收入"
    if kind == "refund":
        return "退款"
    if kind == "transfer" and true_category in {"投资理财", "信用借还"}:
        # 平台内部划转、还款本来就属于这两个分类，不能一律写成「转账红包」。
        return true_category
    if kind == "transfer":
        return "转账红包"
    if kind == "closed":
        return "其他"
    # 个体户：平台只看到「一笔商户收款」，没有真实品类。
    if counterparty_type == "individual":
        return "其他"
    text = f"{counterparty}{item}"
    for word, category in LITERAL_TRAPS:
        if word in text:
            return category
    for word, category in LITERAL_KEYWORDS:
        if word in text:
            return category
    return "其他"


def wechat_tx_type(*, kind: str, counterparty_type: str, tx_no: str,
                   direction: str) -> str:
    """微信「交易类型」：个体户扫码付款大量被写成「商户消费」。"""
    if kind == "refund":
        return "商户消费-退款"
    if kind == "transfer":
        return "转账"
    if direction == "收入":
        return "其他"
    if kind == "closed":
        return "商户消费"
    if counterparty_type == "individual":
        # 70% 被平台粗标成商户消费，30% 保留「扫二维码付款」的形态。
        h = int(hashlib.sha256(tx_no.encode("utf-8")).hexdigest(), 16)
        return "商户消费" if h % 10 < 7 else "扫二维码付款"
    return "商户消费"


# 平台方的交易分类（支付宝口径）
CATEGORIES = {
    "breakfast": "餐饮美食",
    "lunch": "餐饮美食",
    "coffee": "餐饮美食",
    "dinner": "餐饮美食",
    "grocery": "日用百货",
    "dining_out": "餐饮美食",
    "transport": "交通出行",
    "medical": "医疗健康",
    "haircut": "生活服务",
    "subscription": "文化休闲",
    "shopping": "服饰装扮",
    "travel": "文化休闲",
    "housing": "房租物业",
    "parking": "交通出行",
    "tuition": "教育培训",
    "insurance": "保险",
    "utilities": "生活缴费",
    "car": "汽车",
    "tax": "收入",
    "investment": "投资理财",
    "salary": "收入",
    "transfer": "转账红包",
}
