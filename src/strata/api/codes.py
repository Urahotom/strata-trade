"""kabuステーションAPI 公式エラーコード辞書.

出典: 公式ポータル ptal/error.html の3層コード体系。
- リクエストチェックエラー: HTTP 400/401/403 等 + Code (4001xxx〜4005xxx)
- 発注エラー: 発注・訂正・取消APIの Result にセットされる値
未知コードは UnknownCode として扱い、例外にしない（仕様改定対策）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorCodeInfo:
    code: int
    name: str
    message: str


def _describe_request_check(code: int) -> ErrorCodeInfo:
    info = REQUEST_CHECK_CODES.get(code)
    return info or ErrorCodeInfo(
        code, f"UNKNOWN_{code}", f"不明なリクエストチェックエラー ({code})"
    )


def _describe_order_placement(code: int) -> ErrorCodeInfo:
    info = ORDER_PLACEMENT_CODES.get(code)
    return info or ErrorCodeInfo(code, f"UNKNOWN_{code}", f"不明な発注エラー ({code})")


#: 自動売買で特に注意が必要なコードの定数
CODE_API_RUN_LIMIT = 4001006  # API実行回数エラー（スロットリング）
CODE_API_KEY_MISMATCH = 4001009  # APIキー不一致（トークン失効）
CODE_TOKEN_FAILED = 4001013  # トークン取得失敗（パスワード不正）
CODE_REGISTER_LIMIT = 4001018  # 銘柄登録不可（50銘柄上限）
CODE_ONE_SHOT_AMOUNT = 4003001  # ワンショット金額エラー
CODE_ONE_SHOT_QTY = 4003002  # ワンショット数量エラー
CODE_ORDER_NOT_FOUND = 4004001  # 該当注文なし
CODE_CANCEL_NOT_ALLOWED = 4004002  # 取消不可

REQUEST_CHECK_CODES: dict[int, ErrorCodeInfo] = {
    c: ErrorCodeInfo(c, n, m)
    for c, n, m in (
        (4001001, "INTERNAL_ERROR", "内部エラー"),
        (4001002, "NOT_SUPPORTED", "未対応的功能"),
        (4001003, "PARAM_MISSING", "パラメータがありません"),
        (4001004, "PARAM_UNPARSEABLE", "パラメータが不正です"),
        (4001005, "PARAM_CONVERSION", "パラメータの変換えません"),
        (4001006, "API_RUN_LIMIT", "API実行回数エラー"),
        (4001007, "INVALIDATED_API", "許可されていないAPIです"),
        (4001008, "API_NOT_AVAILABLE", "APIが利用できません"),
        (4001009, "API_KEY_MISMATCH", "APIキーが一致しません"),
        (4001010, "MISSING_API_KEY", "APIキーがありません"),
        (4001011, "INVALID_API_KEY_LEN", "APIキーが不正です（文字列長）"),
        (4001012, "INVALID_API_KEY_CHARS", "APIキーが不正です（文字種）"),
        (4001013, "TOKEN_FAILED", "トークン取得失敗（パスワード不正）"),
        (4001014, "METHOD_NOT_ALLOWED", "許可されていないHTTPメソッド"),
        (4001015, "CONTENT_TOO_LARGE", "Content-Lengthが許容値を超えています"),
        (4001016, "INVALIDATED_API", "許可されていないAPIです"),
        (4001017, "LOGIN_AUTH_FAILED", "ログイン認証エラー"),
        (4001018, "REGISTER_LIMIT", "銘柄が登録できません（50銘柄上限）"),
        (4001019, "REGISTER_FAILED", "銘柄が登録できません"),
        (4001020, "UNREGISTER_FAILED", "銘柄が解除できません"),
        (4001021, "UNREGISTER_NOT_FOUND", "銘柄が解除できません（未登録）"),
        (4002001, "SYMBOL_NOT_FOUND", "銘柄が見つかりません"),
        (4002002, "ORDER_TYPE_INVALID", "執行条件が不正です"),
        (4002003, "ORDER_TYPE_INVALID", "執行条件が不正です"),
        (4002004, "SYMBOL_EXCHANGE_MISMATCH", "銘柄と市場が一致しません"),
        (4002005, "PRODUCT_INVALID", "商品が不正です"),
        (4002006, "REGISTER_COUNT_INVALID", "銘柄登録数が不正です"),
        (4002007, "ACCOUNT_TYPE_INVALID", "口座区分が不正です"),
        (4002008, "SIDE_INVALID", "売買区分が不正です"),
        (4002009, "CASH_MARGIN_INVALID", "売買区分が不正です（現物/信用）"),
        (4002010, "DELIV_TYPE_INVALID", "受渡区分が不正です"),
        (4002011, "FUND_TYPE_INVALID", "資産区分が不正です"),
        (4002012, "FRONT_ORDER_TYPE_INVALID", "執行条件が不正です"),
        (4002013, "MARGIN_TRADE_TYPE_INVALID", "信用取引区分が不正です"),
        (4002014, "TIME_IN_FORCE_INVALID", "有効期間条件が不正です"),
        (4002015, "CLOSE_POSITION_DUP", "返済順序指定と返済建玉指定の同時設定"),
        (4002017, "PRICE_INVALID", "値段指定が不正です"),
        (4002018, "IOC_NOT_ALLOWED", "IOCが指定できません"),
        (4002019, "FUND_TYPE_MISSING", "資産区分の指定がありません"),
        (4002020, "PRICE_TYPE_UNSUPPORTED", "価格形式が未対応です"),
        (4002021, "TRADE_DATE_PASSED", "取引期日が経過しています"),
        (4003001, "ONE_SHOT_AMOUNT", "ワンショット金額エラー"),
        (4003002, "ONE_SHOT_QTY", "ワンショット数量エラー"),
        (4004001, "ORDER_NOT_FOUND", "該当する注文がありません"),
        (4004002, "CANCEL_NOT_ALLOWED", "取消できません"),
        (4005001, "RANKING_FAILED", "ランキングの取得に失敗しました"),
        (4005002, "EXCHANGE_FAILED", "為替レートの取得に失敗しました"),
        (4005003, "REGULATIONS_FAILED", "規制情報の取得に失敗しました"),
    )
}

RESULT_NORMAL = 0

ORDER_PLACEMENT_CODES: dict[int, ErrorCodeInfo] = {
    c: ErrorCodeInfo(c, n, m)
    for c, n, m in (
        (0, "NORMAL", "正常"),
        (-1, "ABNORMAL", "異常終了"),
        (2, "ACCOUNT_NOT_FOUND", "口座番号がありません"),
        (5, "EXPIRE_DAY_INVALID", "注文有効期限が不正です"),
        (8, "CLOSE_POSITION_INVALID", "返済建玉情報が不正です"),
        (11, "SYMBOL_INFO_INVALID", "銘柄情報が不正です"),
        (15, "QTY_INVALID", "注文数量が不正です"),
        (16, "QTY_UNIT_INVALID", "注文数量が売買単元に適合していません"),
        (17, "PRICE_TICK_INVALID", "値幅（呼値）が不正です"),
        (18, "PRICE_LIMIT_INVALID", "制限値幅を超えています"),
        (19, "PRICE_TRADING_INVALID", "取引量が不正です"),
        (21, "MARGIN_INSUFFICIENT", "買付可能額（返済可能額）が不足です"),
        (26, "MARGIN_TRADE_INVALID", "信用取引情報が不正です"),
        (32, "ACCOUNT_TYPE_INVALID", "口座区分が不正です"),
        (38, "EXCHANGE_DUP_MSG", "取引所重複電文"),
        (41, "ORDER_NOT_FOUND", "指定期明が存在しません"),
        (42, "EXPIRED", "期限切れです"),
        (43, "ALREADY_FILLED", "約定済みです"),
        (47, "ALREADY_DONE", "終了済みです"),
        (49, "CORRECTION_INVALID", "訂正内容が不正です"),
        (60, "CANCEL_REQUESTED", "取消依頼済みです"),
        (108, "SHORT_SELL_RESTRICT", "空売り規制に抵触します"),
        (109, "TRADE_CLASS_INVALID", "売買区分が不正です"),
        (112, "ORDER_TYPE_INVALID", "執行条件が不正です"),
        (116, "TIME_IN_FORCE_INVALID", "有効期間条件が不正です"),
        (117, "PRICE_INVALID", "値段が不正です"),
        (123, "SOR_AUTO_NOT_ALLOWED", "SOR自動注文ができません"),
        (100051, "HARD_LIMIT", "ハードリミットエラー"),
        (100216, "ONE_SHOT_LIMIT", "ワンショット制限エラー（金額・数量）"),
        (100294, "ORDER_COUNT_LIMIT", "注文個数の上限超過"),
        (100313, "CROSS_TRADE_ORDER", "クロス取引発注エラー（未約定の反対注文あり）"),
        (100314, "CROSS_TRADE_CORRECT", "クロス取引訂正エラー"),
        (100317, "SHORT_SELL_PRICE_RULE", "空売り価格規制（成行）"),
        (100318, "SHORT_SELL_PRICE_RULE", "空売り価格規制"),
        (100319, "SHORT_SELL_PRICE_RULE", "空売り価格規制"),
        (100320, "SHORT_SELL_PRICE_RULE", "空売り価格規制"),
        (100321, "SHORT_SELL_PRICE_RULE", "空売り価格規制"),
        (100322, "SHORT_SELL_PRICE_RULE", "空売り価格規制"),
        (100368, "MARGIN_TSE_SUPPRESSED", "信用新規（東証指定）抑止（SOR/東証+を利用）"),
        (100376, "MARGIN_POSITION_LIMIT", "信用建玉銘柄あたり建玉制限"),
        (100378, "CASH_TSE_SUPPRESSED", "現物買・売（東証指定）抑止（SOR/東証+を利用）"),
    )
}


def describe(code: int) -> ErrorCodeInfo:
    """発注Resultコードを説明する（未知コードはUnknownCode扱い）."""
    return _describe_order_placement(code)


def describe_request_check(code: int) -> ErrorCodeInfo:
    """リクエストチェックコード (4xxxxxx) を説明する."""
    return _describe_request_check(code)
