"""
FMZ交易工具模板类库
包含：通知管理、订单管理、精度管理、ATR计算
"""
import json

# ============================================================
# 1. 通知管理类
# ============================================================
class NotificationManager:
    """管理邮件和APP推送通知"""
    def __init__(self, exchange_obj):
        self.ex = exchange_obj

    def send_notification(self, title, message):
        """
        发送通知 (同时发送邮件和APP推送)
        title: 通知标题
        message: 通知内容
        """
        try:
            # 发送APP推送
            self.ex.IO("push", f"{title}\n{message}")
            Log(f"📱 APP通知已发送: {title}")
        except Exception as e:
            Log(f"⚠️ APP推送失败: {e}", "#FF9900")

        try:
            # 发送邮件
            self.ex.IO("send_email", title, message)
            Log(f"📧 邮件通知已发送: {title}")
        except Exception as e:
            Log(f"⚠️ 邮件发送失败: {e}", "#FF9900")

# ============================================================
# 2. 精度管理类
# ============================================================
class PrecisionManager:
    """管理交易精度"""
    CACHE_FILE = "precision_cache.json"

    def __init__(self, exchange):
        self.ex = exchange
        self.price_precision = 2
        self.amount_precision = 4
        self.min_amount = 0.00001
        self.tick_size = 0.01

    def load_cache(self):
        """加载缓存"""
        try:
            with open(self.CACHE_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}

    def save_cache(self, cache):
        """保存缓存"""
        try:
            with open(self.CACHE_FILE, 'w') as f:
                json.dump(cache, f, indent=2)
        except:
            pass

    def set_precision(self, symbol):
        """设置精度"""
        cache = self.load_cache()
        # 检查缓存
        if symbol in cache:
            data = cache[symbol]
            self.price_precision = int(data['price_precision'])
            self.amount_precision = int(data['amount_precision'])
            self.min_amount = float(data['min_amount'])
            self.tick_size = float(data['tick_size'])
            Log(f"💾 [{symbol}] 从缓存加载精度")
            return True
        # 从交易所获取
        try:
            markets = _C(self.ex.GetMarkets)
            lookup_symbol = f"{symbol}.swap"
            target = markets.get(lookup_symbol)
            if target:
                self.price_precision = int(target['PricePrecision'])
                self.amount_precision = int(target['AmountPrecision'])
                self.min_amount = float(target['MinQty'])
                self.tick_size = float(target['TickSize'])
                # 保存缓存
                cache[symbol] = {
                    'price_precision': self.price_precision,
                    'amount_precision': self.amount_precision,
                    'min_amount': self.min_amount,
                    'tick_size': self.tick_size
                }
                self.save_cache(cache)
                Log(f"✅ [{symbol}] 精度获取成功")
                return True
            else:
                Log(f"❌ 无法获取 {lookup_symbol} 精度")
                return False
        except Exception as e:
            Log(f"❌ 精度获取失败: {e}")
            return False

    def format_price(self, price):
        """格式化价格"""
        return _N(price, self.price_precision)

    def format_amount(self, amount):
        """格式化数量"""
        return _N(amount, self.amount_precision)

# ============================================================
# 3. 订单管理类
# ============================================================
class OrderManager:
    """封装订单管理 - 市价单/限价单用FMZ平台,止损单/跟踪单用币安API"""
    def __init__(self, exchange_obj, precision_mgr):
        self.ex = exchange_obj
        self.precision = precision_mgr
        self.algo_endpoint = "/fapi/v1/algoOrder"  # 新的条件单端点

    def place_market(self, side, quantity):
        """
        下市价单 - 使用FMZ平台方法
        side: "BUY" 或 "SELL"
        """
        try:
            if side == "BUY":
                order = self.ex.Buy(-1, quantity)  # -1表示市价
            else:
                order = self.ex.Sell(-1, quantity)
            if order:
                Log(f"✅ 开仓 {side} {quantity} (市价)")
                return order
            else:
                Log(f"❌ 市价单失败", "#FF0000")
                return None
        except Exception as e:
            Log(f"❌ 市价单异常: {e}", "#FF0000")
            return None

    def place_limit(self, side, quantity, price, reduce_only=False):
        """
        下限价单 - 使用FMZ平台方法
        reduce_only: 仅平仓模式
        """
        try:
            formatted_price = self.precision.format_price(price)
            if side == "BUY":
                if reduce_only:
                    order = self.ex.Buy(formatted_price, quantity, "reduce_only")
                else:
                    order = self.ex.Buy(formatted_price, quantity)
            else:
                if reduce_only:
                    order = self.ex.Sell(formatted_price, quantity, "reduce_only")
                else:
                    order = self.ex.Sell(formatted_price, quantity)
            if order:
                action = "止盈" if reduce_only else "开仓"
                Log(f"✅ {action} {side} {quantity} @ {formatted_price}")
                return order
            else:
                Log(f"❌ 限价单失败", "#FF0000")
                return None
        except Exception as e:
            Log(f"❌ 限价单异常: {e}", "#FF0000")
            return None

    def place_stop_market(self, symbol_api, side, quantity, stop_price, reduce_only=False):
        """
        止损市价单 - 使用新的 Algo Service 端点
        symbol_api: 币安API格式的币种名(如 BTCUSDT)
        side: "BUY" 或 "SELL"
        stop_price: 触发价格
        reduce_only: 是否仅平仓
        """
        formatted_stop = self.precision.format_price(stop_price)
        params = (
            f"algoType=CONDITIONAL"
            f"&symbol={symbol_api}"
            f"&side={side}"
            f"&type=STOP_MARKET"
            f"&quantity={quantity}"
            f"&triggerPrice={formatted_stop}"
            f"&workingType=CONTRACT_PRICE"
        )
        if reduce_only:
            params += "&reduceOnly=true"
        action = "止损" if reduce_only else "加仓"
        Log(f"✅ {action}单 {side} {quantity} @ {formatted_stop}")
        return self._api_request(self.algo_endpoint, params, "POST")

    def place_trailing_stop(self, symbol_api, side, quantity, callback_rate, activation_price=0, reduce_only=False):
        """
        跟踪止损单 - 使用新的 Algo Service 端点
        symbol_api: 币安API格式的币种名
        side: "BUY" 或 "SELL"
        callback_rate: 回调率百分比(如 1.5 表示1.5%)
        activation_price: 激活价格(可选,0表示立即激活)
        reduce_only: 是否仅平仓
        """
        params = (
            f"algoType=CONDITIONAL"
            f"&symbol={symbol_api}"
            f"&side={side}"
            f"&type=TRAILING_STOP_MARKET"
            f"&quantity={quantity}"
            f"&callbackRate={callback_rate}"
        )
        if activation_price > 0:
            formatted_activation = self.precision.format_price(activation_price)
            params += f"&activatePrice={formatted_activation}"
        else:
            formatted_activation = 0
        if reduce_only:
            params += "&reduceOnly=true"
        action = "跟踪止盈" if reduce_only else "跟踪开仓"
        if activation_price > 0:
            Log(f"✅ {action} {side} {quantity} 激活价={formatted_activation} 回调={callback_rate}%")
        else:
            Log(f"✅ {action} {side} {quantity} 回调={callback_rate}%")
        return self._api_request(self.algo_endpoint, params, "POST")

    def cancel_order(self, order_id):
        """
        撤销单个订单 - 使用FMZ平台方法
        order_id: FMZ平台返回的订单ID
        """
        try:
            result = _C(self.ex.CancelOrder, order_id)
            if result:
                Log(f"✅ 订单已撤销: ID={order_id}")
                return True
            else:
                Log(f"⚠️ 撤单失败: ID={order_id}", "#FF9900")
                return False
        except Exception as e:
            error_msg = str(e)
            # 如果订单已成交或不存在,不算错误
            if "Unknown order" in error_msg or "-2011" in error_msg:
                Log(f"📭 订单不存在或已成交: ID={order_id}")
                return True
            else:
                Log(f"❌ 撤单异常: {e}", "#FF0000")
                return False

    def cancel_all_orders(self, symbol_fmz, symbol_api):
        """
        撤销所有挂单 - 包括FMZ平台订单和Algo条件单
        symbol_fmz: FMZ格式的币种名 (如 BTC_USDT)
        symbol_api: 币安API格式的币种名 (如 BTCUSDT)
        """
        fmz_count = 0
        algo_count = 0
        # 1. 撤销FMZ平台的普通订单(市价单/限价单)
        try:
            self.ex.SetCurrency(symbol_fmz)
            orders = _C(self.ex.GetOrders, f"{symbol_fmz}.swap")
            if orders and len(orders) > 0:
                for order in orders:
                    result = self.ex.CancelOrder(order['Id'])
                    if result:
                        fmz_count += 1
                    Sleep(200)
        except Exception as e:
            pass
        Sleep(300)
        # 2. 撤销Algo条件单(止损单/跟踪单)
        try:
            params = f"symbol={symbol_api}"
            for i in range(3):
                try:
                    ret = self.ex.IO("api", "DELETE", "/fapi/v1/algoOpenOrders", params)
                    if ret:
                        algo_count = 1
                        break
                except Exception as e:
                    error_msg = str(e)
                    if "No open algo order" in error_msg or "-1200" in error_msg:
                        break
                    else:
                        Sleep(500)
        except Exception as e:
            pass
        total = fmz_count + algo_count
        if total > 0:
            Log(f"✅ 撤单完成 (FMZ:{fmz_count} Algo:{algo_count})")
        return True

    def _api_request(self, endpoint, params, method="POST"):
        """
        通用API请求(用于止损单和跟踪单)
        """
        for i in range(3):
            try:
                ret = self.ex.IO("api", method, endpoint, params)
                if ret:
                    return ret
            except Exception as e:
                if i == 2:  # 最后一次才报错
                    Log(f"❌ API请求失败: {e}", "#FF0000")
                Sleep(500)
        return None

# ============================================================
# 4. ATR计算工具
# ============================================================
class ATRCalculator:
    """ATR计算工具类"""

    @staticmethod
    def get_atr(exchange, symbol, period=20, exclude_today=True):
        """
        获取ATR值
        exchange: 交易所对象
        symbol: 币种符号
        period: ATR周期
        exclude_today: True时排除今日K线,使用前20日数据
        """
        try:
            # 必须先设置合约类型，再设置币种
            exchange.SetContractType("swap")
            exchange.SetCurrency(symbol)
            # 使用 _C() 包装 GetRecords，提供自动重试机制
            records = _C(exchange.GetRecords, PERIOD_D1)
            if not records or len(records) < period + 2:
                Log(f"⚠️ K线数据不足: 需要{period+2}根，实际{len(records) if records else 0}根")
                return None
            atr_array = TA.ATR(records, period)
            if exclude_today:
                # 使用倒数第2根K线(昨日)的ATR, 排除今日未完成K线
                return atr_array[-2]
            else:
                return atr_array[-1]
        except Exception as e:
            Log(f"❌ ATR计算失败: {e}")
            return None

    @staticmethod
    def get_atr_by_percentage(current_price, percentage):
        """
        根据价格百分比计算ATR
        current_price: 当前价格
        percentage: 百分比 (如50表示50%)
        """
        return current_price * (percentage / 100)

# ============================================================
# 导出函数 (FMZ模板类库必须)
# ============================================================
def init():
    """
    初始化函数 - FMZ平台调用此函数获取类实例
    返回一个包含所有工具类的字典
    """
    return {
        'NotificationManager': NotificationManager,
        'PrecisionManager': PrecisionManager,
        'OrderManager': OrderManager,
        'ATRCalculator': ATRCalculator
    }
