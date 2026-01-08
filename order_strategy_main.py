import time
import json
# ============================================================
# 基于交易所挂单的趋势策略 - 主策略文件
# 核心思想: 通过交易所原生订单(止损单、跟踪单、限价单)实现策略
# 不断检查仓位变化，根据仓位变化判断状态转换
# ============================================================

# 常量定义
MY_SYMBOLS = ["BTC_USDT", "ETH_USDT", "SOL_USDT",
              "ZEC_USDT","1000PEPE_USDT","DOGE_USDT",
              "XRP_USDT","币安人生_USDT"]

# 策略参数（实盘）
STRATEGY_CONFIG = {
    'entry_callback':0.12,
    'atr_period': 20,       # ATR周期
    'sl_for_size': 0.4,    # 用于计算开仓大小的ATR倍数
    'base_position_pct': 0.4,  # 底仓百分比: 40%
    'add_position_pct': 0.6,   # 加仓百分比: 60%
    'sl_atr': 0.6,          # 底仓止损: -0.6 ATR
    'add_trigger': 0.1,     # 加仓触发: 0.1 ATR
    'protective_sl_trigger': 0.2,  # 保护性止损触发: 底仓浮盈 +0.2 ATR
    'protective_sl_offset': 0.2,   # 保护性止损位置: -0.2 ATR
    'full_sl_atr': 0.3,     # 满仓止损: -0.3 ATR
    'trail_activation': 0.28, # 跟踪止盈激活: 0.28 ATR
    'trail_callback': 0.15,  # 跟踪止盈回调: 0.15 ATR
    # 小波动模式 (0): 直接在+0.3ATR走90%
    'volatility_small': [
        {'atr': 0.3, 'pct': 0.9}
    ],
    # 中波动模式 (1): +0.35ATR (25%), +0.5ATR (45%), +0.65ATR (20%)
    'volatility_medium': [
        {'atr': 0.35, 'pct': 0.25},
        {'atr': 0.5, 'pct': 0.45},
        {'atr': 0.65, 'pct': 0.2}
    ],
    # 大波动模式 (2): +0.65ATR走80%
    'volatility_large': [
        {'atr': 0.65, 'pct': 0.8}
    ]
}

# ============================================================
# 核心策略管理器
# ============================================================
class OrderBasedStrategyManager:
    """基于交易所挂单的策略管理器"""
    def __init__(self, exchange, config):
        self.ex = exchange
        self.cfg = config
        # 从模板类库导入工具类 (通过ext对象直接调用)
        self.precision_mgr = ext.PrecisionManager(exchange)
        self.order_mgr = ext.OrderManager(exchange, self.precision_mgr)
        self.notif_mgr = ext.NotificationManager(exchange)
        self.atr_calc = ext.ATRCalculator
        # 策略状态
        self.state = "IDLE"
        self.symbol = ""
        self.symbol_for_api = ""  # 交易所API使用的币种格式 (如 BTCUSDT)
        self.direction = 0  # 1=做多, -1=做空
        self.max_loss = 0
        self.entry_mode = 0  # 1=市价, 2=限价, 3=市价激活跟踪, 4=限价激活跟踪
        self.entry_limit_price = 0
        self.volatility_mode = 1  # 0=小波动, 1=中波动, 2=大波动
        # ATR和仓位计算
        self.atr_val = 0
        self.full_amount = 0  # 满仓数量
        self.base_price = 0   # 底仓均价
        # 上次检查的仓位
        self.last_position_amount = 0
        # 确认信息存储
        self.pending_confirm_info = {}
        # 保护性止损标志
        self.protective_sl_placed = False
        # 入场配置信息（用于策略状态展示）
        self.entry_config = {
            'volatility_desc': '',
            'atr_mode': '',
            'atr_value': 0,
            'entry_mode_desc': ''
        }

    def _reset(self):
        """重置策略状态"""
        if self.symbol:
            # 撤销所有挂单 - 包括FMZ订单和Algo订单
            Log("🔄 撤销所有挂单...", "#FFA500")
            self.order_mgr.cancel_all_orders(self.symbol, self.symbol_for_api)
            Sleep(500)  # 等待撤单完成
            # 二次确认撤单（防止网络延迟导致撤单失败）
            self.order_mgr.cancel_all_orders(self.symbol, self.symbol_for_api)
        self.state = "IDLE"
        self.symbol = ""
        self.symbol_for_api = ""
        self.direction = 0
        self.max_loss = 0
        self.entry_mode = 0
        self.entry_limit_price = 0
        self.volatility_mode = 1
        self.atr_val = 0
        self.full_amount = 0
        self.base_price = 0
        self.last_position_amount = 0
        self.pending_confirm_info = {}
        self.protective_sl_placed = False
        self.entry_config = {
            'volatility_desc': '',
            'atr_mode': '',
            'atr_value': 0,
            'entry_mode_desc': ''
        }
        Log("🔄 策略已重置")

    def _convert_symbol_for_api(self, symbol):
        """
        转换币种格式用于API调用
        BTC_USDT -> BTCUSDT
        """
        return symbol.replace("_", "")

    def start_entry(self, symbol, direction_str, max_loss, entry_mode, limit_price=0, volatility_mode=1, atr_percentage=0):
        """
        启动入场流程
        direction_str: "buy" 或 "sell"
        entry_mode: 1=市价, 2=限价, 3=市价激活跟踪, 4=限价激活跟踪
        volatility_mode: 0=小波动, 1=中波动, 2=大波动
        atr_percentage: ATR百分比(如50表示50%)，0或不传时使用默认周期(20)
        """
        if self.state != "IDLE":
            Log("⚠️ 策略正在运行中", "#FF9900")
            return False
        self.symbol = symbol
        self.symbol_for_api = self._convert_symbol_for_api(symbol)
        # 必须先设置合约类型，再设置币种
        self.ex.SetContractType("swap")
        self.ex.SetCurrency(symbol)
        # 设置精度
        if not self.precision_mgr.set_precision(symbol):
            Log("❌ 精度设置失败")
            self._reset()
            return False
        # 获取当前价格
        ticker = _C(self.ex.GetTicker)
        current_price = ticker['Last']
        # 计算ATR值
        if atr_percentage > 0:
            # 使用百分比模式: ATR = 当前价格 * (百分比 / 100)
            self.atr_val = self.atr_calc.get_atr_by_percentage(current_price, atr_percentage)
            Log(f"📊 使用ATR百分比模式: {atr_percentage}% → ATR = {self.atr_val:.2f}")
        else:
            # 使用传统周期模式
            actual_atr_period = self.cfg['atr_period']
            Log(f"📊 使用ATR周期模式: {actual_atr_period}天")
            self.atr_val = self.atr_calc.get_atr(self.ex, symbol, actual_atr_period, exclude_today=True)
            if not self.atr_val:
                Log("❌ ATR计算失败")
                self._reset()
                return False
        # 计算满仓数量
        raw_size = max_loss / (self.cfg['sl_for_size'] * self.atr_val)
        self.full_amount = self.precision_mgr.format_amount(raw_size)
        self.direction = 1 if direction_str == "buy" else -1
        self.max_loss = max_loss
        self.entry_mode = entry_mode
        self.entry_limit_price = limit_price
        self.volatility_mode = volatility_mode
        # 保存确认信息
        base_amount = self.precision_mgr.format_amount(self.full_amount * self.cfg['base_position_pct'])
        volatility_desc = {0: '小波动', 1: '中波动', 2: '大波动'}[volatility_mode]
        self.pending_confirm_info = {
            'symbol': symbol,
            'direction': '做多 🟢' if self.direction == 1 else '做空 🔴',
            'mode': entry_mode,
            'mode_desc': {1: '市价入场', 2: '限价入场', 3: '市价激活跟踪入场', 4: '限价激活跟踪入场'}[entry_mode],
            'volatility_mode': volatility_mode,
            'volatility_desc': volatility_desc,
            'limit_price': limit_price,
            'current_price': current_price,
            'atr': self.atr_val,
            'atr_mode': 'percentage' if atr_percentage > 0 else 'period',
            'atr_value': atr_percentage if atr_percentage > 0 else self.cfg['atr_period'],
            'max_loss': max_loss,
            'base_amount': base_amount,
            'full_amount': self.full_amount,
            'base_value': base_amount * current_price,
            'full_value': self.full_amount * current_price,
            'base_pct': int(self.cfg['base_position_pct'] * 100),
            'add_pct': int(self.cfg['add_position_pct'] * 100)
        }
        self.state = "WAIT_CONFIRM"
        Log(f"✅ 入场参数设置完成，等待确认", "#00BFFF")
        return True

    def get_confirm_info(self):
        """获取确认信息"""
        if self.state != "WAIT_CONFIRM":
            return []
        info = self.pending_confirm_info
        lines = [
            "=" * 50,
            "⚠️  请确认开仓信息",
            "=" * 50,
            "",
            f"币种: {info['symbol']}",
            f"方向: {info['direction']}",
            f"入场模式: {info['mode_desc']}",
            f"波动模式: {info['volatility_desc']}",
        ]
        if info['mode'] in [2, 4]:
            lines.append(f"触发价格: {info['limit_price']}")
        lines.append(f"当前价格: {info['current_price']}")
        # 显示ATR计算方式
        if info['atr_mode'] == 'percentage':
            lines.append(f"ATR模式: 百分比 {info['atr_value']}%")
        else:
            lines.append(f"ATR模式: 周期 {info['atr_value']}天")
        lines.extend([
            f"ATR值: {info['atr']}",
            f"最大亏损: {info['max_loss']} USDT",
            "",
            "-" * 50,
            f"底仓数量: {info['base_amount']} ({info['base_pct']}%)",
            f"底仓价值: {info['base_value']} USDT",
            f"加仓比例: {info['add_pct']}%",
            "",
            f"满仓数量: {info['full_amount']} (100%)",
            f"满仓价值: {info['full_value']} USDT",
            "",
            "=" * 50,
            "⚠️  请点击【✅ 确认开仓】或【❌ 取消】"
        ])
        return lines

    def confirm_entry(self):
        """确认开仓"""
        if self.state != "WAIT_CONFIRM":
            Log("❌ 当前不在确认状态", "#FF0000")
            return False
        Log("✅ 用户确认开仓，开始挂单", "#00FF00")

        # 保存入场配置信息
        info = self.pending_confirm_info
        self.entry_config = {
            'volatility_desc': info['volatility_desc'],
            'atr_mode': '百分比模式' if info['atr_mode'] == 'percentage' else '周期模式',
            'atr_value': info['atr_value'],
            'entry_mode_desc': info['mode_desc']
        }

        # 计算底仓数量
        base_amount = self.precision_mgr.format_amount(self.full_amount * self.cfg['base_position_pct'])
        side = "BUY" if self.direction == 1 else "SELL"
        # 根据入场模式执行
        if self.entry_mode == 1:
            # 模式1: 市价入场
            Log("🚀 模式1: 市价入场")
            res = self.order_mgr.place_market(side, base_amount)
            if res:
                Log(f"✅ 市价单已提交: {res}")
                self.state = "WAIT_ENTRY"
            else:
                Log("❌ 市价单提交失败", "#FF0000")
                self._reset()
                return False
        elif self.entry_mode == 2:
            # 模式2: 限价入场
            Log(f"📌 模式2: 限价入场 @ {self.entry_limit_price}")
            res = self.order_mgr.place_limit(side, base_amount, self.entry_limit_price)
            if res:
                Log(f"✅ 限价单已提交: {res}")
                self.state = "WAIT_ENTRY"
            else:
                Log("❌ 限价单提交失败", "#FF0000")
                self._reset()
                return False
        elif self.entry_mode == 3:
            # 模式3: 市价激活跟踪入场
            Log("🎣 模式3: 市价激活跟踪入场")
            # 获取当前价格作为参考点
            ticker = _C(self.ex.GetTicker)
            current_price = ticker['Last']
            # 回调距离 = 0.1 ATR (配置值) * ATR值
            callback_distance = self.cfg['entry_callback'] * self.atr_val
            # 回调率 = 回调距离 / 当前价格 * 100%
            callback_rate = (callback_distance / current_price) * 100
            # 限制回调率范围 0.1-5%
            callback_rate = max(0.1, min(5.0, callback_rate))
            # 保留两位小数
            callback_rate = _N(callback_rate, 2)
            Log(f"📊 入场跟踪单: 回调距离={callback_distance:.2f}, 回调率={callback_rate:.2f}%")
            res = self.order_mgr.place_trailing_stop(self.symbol_for_api, side, base_amount, callback_rate, 0)
            if res:
                Log(f"✅ 跟踪单已提交: {res}")
                self.state = "WAIT_ENTRY"
            else:
                Log("❌ 跟踪单提交失败", "#FF0000")
                self._reset()
                return False
        elif self.entry_mode == 4:
            # 模式4: 限价激活跟踪入场
            Log(f"🎣 模式4: 限价激活跟踪入场, 激活价={self.entry_limit_price}")
            formatted_price = self.precision_mgr.format_price(self.entry_limit_price)
            # 回调距离 = 0.1 ATR (配置值) * ATR值
            callback_distance = self.cfg['entry_callback'] * self.atr_val
            # 回调率 = 回调距离 / 激活价格 * 100%
            callback_rate = (callback_distance / self.entry_limit_price) * 100
            callback_rate = max(0.1, min(5.0, callback_rate))
            # 保留两位小数
            callback_rate = _N(callback_rate, 2)
            Log(f"📊 入场跟踪单: 激活价={self.entry_limit_price}, 回调距离={callback_distance:.2f}, 回调率={callback_rate:.2f}%")
            res = self.order_mgr.place_trailing_stop(self.symbol_for_api, side, base_amount, callback_rate, formatted_price)
            if res:
                Log(f"✅ 限价跟踪单已提交: {res}")
                self.state = "WAIT_ENTRY"
            else:
                Log("❌ 限价跟踪单提交失败", "#FF0000")
                self._reset()
                return False
        return True

    def cancel_entry(self):
        """取消开仓"""
        if self.state not in ["WAIT_CONFIRM", "WAIT_ENTRY"]:
            Log("❌ 当前不在可取消状态", "#FF0000")
            return False
        Log("❌ 用户取消开仓", "#FF9900")
        self._reset()
        return True

    def _get_position_amount(self):
        """获取当前持仓数量"""
        try:
            # 防御性编程：确保在调用GetPosition前已设置合约类型和币种
            if self.symbol:
                self.ex.SetContractType("swap")
                self.ex.SetCurrency(self.symbol)
            positions = _C(self.ex.GetPosition)
            target_type = PD_LONG if self.direction == 1 else PD_SHORT
            for p in positions:
                if p['Type'] == target_type and p['Amount'] > 0:
                    return p['Amount'], p['Price']
            return 0, 0
        except Exception as e:
            Log(f"⚠️ 获取持仓失败: {e}")
            return None, None

    def _send_stop_loss_notification(self, sl_price):
        """发送止损通知"""
        direction_str = "做多🟢" if self.direction == 1 else "做空🔴"
        loss_pct = ((sl_price - self.base_price) / self.base_price * 100) * self.direction
        loss_amount = (sl_price - self.base_price) * self.last_position_amount * self.direction

        notif_title = f"🛑 底仓止损触发 - {self.symbol}"
        notif_msg = (
            f"币种: {self.symbol}\n"
            f"方向: {direction_str}\n"
            f"底仓价格: {self.base_price}\n"
            f"止损价格: {sl_price}\n"
            f"亏损率: {loss_pct}%\n"
            f"亏损金额: {loss_amount} USDT\n"
            f"时间: {_D()}"
        )
        self.notif_mgr.send_notification(notif_title, notif_msg)

    def _send_base_entry_notification(self, current_amount, current_price):
        """发送底仓开仓通知"""
        direction_str = "做多🟢" if self.direction == 1 else "做空🔴"
        notif_title = f"✅ 底仓开仓成功 - {self.symbol}"
        notif_msg = (
            f"币种: {self.symbol}\n"
            f"方向: {direction_str}\n"
            f"开仓价格: {current_price}\n"
            f"底仓数量: {current_amount}\n"
            f"底仓价值: {current_amount * current_price} USDT\n"
            f"ATR: {self.atr_val}\n"
            f"时间: {_D()}"
        )
        self.notif_mgr.send_notification(notif_title, notif_msg)

    def _send_add_position_notification(self, current_amount, current_price):
        """发送加仓通知"""
        direction_str = "做多🟢" if self.direction == 1 else "做空🔴"
        profit_pct = ((current_price - self.base_price) / self.base_price * 100) * self.direction
        notif_title = f"✅ 加仓成功 - {self.symbol}"
        notif_msg = (
            f"币种: {self.symbol}\n"
            f"方向: {direction_str}\n"
            f"底仓价格: {self.base_price}\n"
            f"当前价格: {current_price}\n"
            f"浮盈率: {profit_pct}%\n"
            f"满仓数量: {current_amount}\n"
            f"满仓价值: {current_amount * current_price} USDT\n"
            f"时间: {_D()}"
        )
        self.notif_mgr.send_notification(notif_title, notif_msg)

    def _send_close_position_notification(self, close_price):
        """发送平仓通知"""
        direction_str = "做多🟢" if self.direction == 1 else "做空🔴"
        profit_pct = ((close_price - self.base_price) / self.base_price * 100) * self.direction
        profit_amount = (close_price - self.base_price) * self.last_position_amount * self.direction

        result_emoji = "✅" if profit_amount > 0 else "❌"
        notif_title = f"{result_emoji} 平仓完成 - {self.symbol}"
        notif_msg = (
            f"币种: {self.symbol}\n"
            f"方向: {direction_str}\n"
            f"底仓价格: {self.base_price}\n"
            f"平仓价格: {close_price}\n"
            f"盈亏率: {profit_pct}%\n"
            f"盈亏金额: {profit_amount} USDT\n"
            f"持仓数量: {self.last_position_amount}\n"
            f"时间: {_D()}"
        )
        self.notif_mgr.send_notification(notif_title, notif_msg)

    def _handle_wait_entry_state(self, current_amount, current_price, expected_base, tolerance):
        """
        处理 WAIT_ENTRY 状态: 等待底仓建立
        """
        # 检查异常情况：上次有仓位但现在归零（手动平仓或其他原因）
        if self.last_position_amount > 0 and current_amount == 0:
            Log(f"⚠️ 入场阶段仓位归零，策略重置", "#FF9900")
            self._reset()
            return

        # 上次无仓位，现在有底仓
        if self.last_position_amount == 0 and abs(current_amount - expected_base) < tolerance:
            Log(f"✅ 底仓建立 {current_amount:.4f} @ {current_price:.2f}", "#00FF00")
            self.base_price = current_price
            self.last_position_amount = current_amount
            self.state = "ENTRY_DONE"

            # 发送开仓通知
            self._send_base_entry_notification(current_amount, current_price)

            # 执行步骤3的挂单动作
            self._place_orders_after_base_entry()

    def _handle_entry_done_state(self, current_amount, current_price, expected_base, expected_full, tolerance):
        """
        处理 ENTRY_DONE 状态: 底仓已建立，等待加仓或止损
        """
        # 检查仓位归零（止损触发）
        if self.last_position_amount > 0 and current_amount == 0:
            ticker = _C(self.ex.GetTicker)
            sl_price = ticker['Last']

            # 发送止损通知
            self._send_stop_loss_notification(sl_price)

            Log(f"🛑 底仓止损触发，全部平仓", "#FF0000")
            self._reset()
            return

        # 上次底仓，现在满仓（加仓完成）
        if abs(self.last_position_amount - expected_base) < tolerance and abs(current_amount - expected_full) < tolerance:
            Log(f"✅ 加仓完成 {current_amount:.4f}", "#00FF00")

            # 获取当前价格
            ticker = _C(self.ex.GetTicker)
            current_price = ticker['Last']

            # 发送加仓通知
            self._send_add_position_notification(current_amount, current_price)

            self.last_position_amount = current_amount
            self.state = "WAIT_EXIT"

            # 执行步骤4的挂单动作
            self._place_orders_after_full_position()

    def _handle_wait_exit_state(self, current_amount, current_price):
        """
        处理 WAIT_EXIT 状态: 满仓已建立，等待平仓或保护性止损
        """
        # 先检查仓位归零（最高优先级）
        if self.last_position_amount > 0 and current_amount == 0:
            ticker = _C(self.ex.GetTicker)
            close_price = ticker['Last']

            # 发送平仓通知
            self._send_close_position_notification(close_price)

            Log(f"✅ 全部平仓，策略完成", "#00FF00")
            self._reset()
            return

        # 再检查保护性止损触发条件（仅在有仓位情况下检查）
        if not self.protective_sl_placed and current_amount > 0:
            self._check_and_place_protective_sl(current_price, current_amount)

    def check_position_and_update_state(self):
        """
        核心逻辑: 每2秒检查仓位变化，根据变化判断状态
        """
        if self.state == "IDLE" or self.state == "WAIT_CONFIRM":
            return

        # 确保使用正确的币种
        if self.symbol:
            self.ex.SetContractType("swap")
            self.ex.SetCurrency(self.symbol)

        # 获取当前持仓
        current_amount, current_price = self._get_position_amount()
        if current_amount is None:
            return  # 获取失败，跳过本轮

        # 计算预期的底仓和满仓数量
        expected_base = self.precision_mgr.format_amount(self.full_amount * self.cfg['base_position_pct'])
        expected_full = self.full_amount

        # 定义一个容差 (考虑精度误差)
        tolerance = self.precision_mgr.min_amount * 2

        # 根据当前状态分发到对应的处理函数
        if self.state == "WAIT_ENTRY":
            self._handle_wait_entry_state(current_amount, current_price, expected_base, tolerance)
        elif self.state == "ENTRY_DONE":
            self._handle_entry_done_state(current_amount, current_price, expected_base, expected_full, tolerance)
        elif self.state == "WAIT_EXIT":
            self._handle_wait_exit_state(current_amount, current_price)

    def _check_and_place_protective_sl(self, current_price, current_amount):
        """
        检查并挂保护性止损单
        当底仓浮盈达到 +0.2 ATR 时，撤销所有订单并重新挂单：
        - 新的保护性止损单（-0.2 ATR，满仓）
        - 重新挂跟踪止盈单（参数不变）
        - 重新挂所有限价止盈单（参数不变）
        """
        # 计算触发价格 (底仓价格 + 方向 * 0.2 ATR)
        trigger_price = self.base_price + (self.direction * self.cfg['protective_sl_trigger'] * self.atr_val)
        # 检查是否达到触发条件
        if self.direction == 1:  # 做多
            reached = current_price >= trigger_price
        else:  # 做空
            reached = current_price <= trigger_price
        if reached:
            Log(f"🛡️ 底仓浮盈达到 +{self.cfg['protective_sl_trigger']} ATR，更新为保护性止损", "#00BFFF")
            # 1. 撤销所有订单（FMZ订单和Algo订单）
            self.order_mgr.cancel_all_orders(self.symbol, self.symbol_for_api)
            Sleep(500)

            # 2. 挂保护性止损单 (-0.2 ATR, 使用当前确切的仓位数量)
            protective_sl_price = self.base_price - (self.direction * self.cfg['protective_sl_offset'] * self.atr_val)
            protective_sl_price = self.precision_mgr.format_price(protective_sl_price)
            sl_side = "SELL" if self.direction == 1 else "BUY"
            # 使用确切的当前仓位数量，而不是 self.full_amount
            current_amount_formatted = self.precision_mgr.format_amount(current_amount)
            self.order_mgr.place_stop_market(self.symbol_for_api, sl_side, current_amount_formatted, protective_sl_price, reduce_only=True)

            # 3. 重新挂跟踪止盈单 (激活价0.28 ATR, 回调0.15 ATR, 使用当前确切的仓位数量)
            trail_activation = self.base_price + (self.direction * self.cfg['trail_activation'] * self.atr_val)
            trail_activation = self.precision_mgr.format_price(trail_activation)
            callback_distance = self.cfg['trail_callback'] * self.atr_val
            callback_rate = (callback_distance / trail_activation) * 100
            callback_rate = max(0.1, min(5.0, callback_rate))
            callback_rate = _N(callback_rate, 2)
            close_side = "SELL" if self.direction == 1 else "BUY"
            # 使用当前确切的仓位数量，而不是 self.full_amount
            self.order_mgr.place_trailing_stop(
                self.symbol_for_api,
                close_side,
                current_amount_formatted,
                callback_rate,
                trail_activation,
                reduce_only=True
            )

            # 4. 重新挂限价止盈单
            self._place_tp_orders(close_side)

            self.protective_sl_placed = True
            Log(f"✅ 保护性止损体系已建立: 止损 @ {protective_sl_price}", "#00FF00")

    def _place_tp_orders(self, close_side):
        """
        根据波动模式挂限价止盈单
        close_side: "SELL" (做多平仓) 或 "BUY" (做空平仓)
        """
        volatility_key = {0: 'volatility_small', 1: 'volatility_medium', 2: 'volatility_large'}[self.volatility_mode]
        tp_configs = self.cfg[volatility_key]
        for idx, tp_config in enumerate(tp_configs, 1):
            atr_mult = tp_config['atr']
            pct = tp_config['pct']
            tp_price = self.base_price + (self.direction * atr_mult * self.atr_val)
            tp_price = self.precision_mgr.format_price(tp_price)
            tp_amount = self.precision_mgr.format_amount(self.full_amount * pct)
            res_tp = self.order_mgr.place_limit(close_side, tp_amount, tp_price, reduce_only=True)
            if not res_tp:
                Log(f"⚠️ 止盈{idx}挂单失败", "#FF9900")

    def _place_orders_after_base_entry(self):
        """
        步骤3: 底仓建立后的挂单动作
        - 挂止损单 (-0.6 ATR, 底仓数量)
        - 挂条件委托单 (浮盈0.1 ATR时市价加仓)
        """
        # 止损单
        base_amount = self.precision_mgr.format_amount(self.full_amount * self.cfg['base_position_pct'])
        sl_price = self.base_price - (self.direction * self.cfg['sl_atr'] * self.atr_val)
        sl_price = self.precision_mgr.format_price(sl_price)
        # 止损方向: 做多时止损=卖出(SELL), 做空时止损=买入(BUY)
        sl_side = "SELL" if self.direction == 1 else "BUY"
        res_sl = self.order_mgr.place_stop_market(self.symbol_for_api, sl_side, base_amount, sl_price, reduce_only=True)
        if not res_sl:
            Log("⚠️ 止损单挂单失败", "#FF9900")
        # 条件委托单: 浮盈0.1 ATR时市价加仓
        add_trigger_price = self.base_price + (self.direction * self.cfg['add_trigger'] * self.atr_val)
        add_trigger_price = self.precision_mgr.format_price(add_trigger_price)
        # 加仓数量 = 满仓 * 加仓百分比
        add_amount = self.precision_mgr.format_amount(self.full_amount * self.cfg['add_position_pct'])
        # 加仓方向: 做多时加仓=买入(BUY), 做空时加仓=卖出(SELL)
        add_side = "BUY" if self.direction == 1 else "SELL"
        res_add = self.order_mgr.place_stop_market(self.symbol_for_api, add_side, add_amount, add_trigger_price)
        if not res_add:
            Log("⚠️ 加仓触发单挂单失败", "#FF9900")

    def _place_orders_after_full_position(self):
        """
        步骤4: 满仓后的挂单动作
        - 撤销原有止损单
        - 挂新止损单 (-0.3 ATR, 满仓)
        - 挂跟踪单平仓 (激活价0.3 ATR, 回调0.15 ATR, 满仓)
        - 挂3个限价止盈单
        """
        # 先撤销所有挂单 - 包括FMZ订单和Algo订单
        self.order_mgr.cancel_all_orders(self.symbol, self.symbol_for_api)
        Sleep(500)
        # 1. 新止损单 (-0.3 ATR, 满仓)
        full_sl_price = self.base_price - (self.direction * self.cfg['full_sl_atr'] * self.atr_val)
        full_sl_price = self.precision_mgr.format_price(full_sl_price)
        sl_side = "SELL" if self.direction == 1 else "BUY"
        self.order_mgr.place_stop_market(self.symbol_for_api, sl_side, self.full_amount, full_sl_price, reduce_only=True)
        # 2. 跟踪单平仓 (激活价0.3 ATR, 回调0.15 ATR)
        trail_activation = self.base_price + (self.direction * self.cfg['trail_activation'] * self.atr_val)
        trail_activation = self.precision_mgr.format_price(trail_activation)
        callback_distance = self.cfg['trail_callback'] * self.atr_val
        callback_rate = (callback_distance / trail_activation) * 100
        callback_rate = max(0.1, min(5.0, callback_rate))
        callback_rate = _N(callback_rate, 2)
        close_side = "SELL" if self.direction == 1 else "BUY"
        self.order_mgr.place_trailing_stop(
            self.symbol_for_api,
            close_side,
            self.full_amount,
            callback_rate,
            trail_activation,
            reduce_only=True
        )
        # 3. 挂限价止盈单
        self._place_tp_orders(close_side)

    def get_status_info(self):
        """获取状态信息"""
        lines = ["=" * 50]
        if self.state == "IDLE":
            lines.append("当前状态: 空闲")
        else:
            lines.append(f"当前状态: {self.state}")
            lines.append(f"币种: {self.symbol}")
            lines.append(f"方向: {'做多 🟢' if self.direction == 1 else '做空 🔴'}")
            lines.append("")
            lines.append("-" * 50)
            lines.append("📋 入场配置信息")
            lines.append("-" * 50)
            if self.entry_config['entry_mode_desc']:
                lines.append(f"入场模式: {self.entry_config['entry_mode_desc']}")
            if self.entry_config['volatility_desc']:
                lines.append(f"波动模式: {self.entry_config['volatility_desc']}")
            if self.entry_config['atr_mode']:
                if self.entry_config['atr_mode'] == '百分比模式':
                    lines.append(f"ATR模式: {self.entry_config['atr_mode']} ({self.entry_config['atr_value']}%)")
                else:
                    lines.append(f"ATR模式: {self.entry_config['atr_mode']} ({int(self.entry_config['atr_value'])}天)")
            lines.append("")
            lines.append("-" * 50)
            lines.append("📊 实时数据")
            lines.append("-" * 50)
            lines.append(f"ATR值: {self.atr_val}")
            lines.append(f"满仓数量: {self.full_amount}")
            if self.base_price > 0:
                lines.append(f"底仓均价: {self.base_price}")
            if self.last_position_amount > 0:
                lines.append(f"当前持仓: {self.last_position_amount}")
        lines.append("=" * 50)
        return "\n".join(lines)

# ============================================================
# 主程序
# ============================================================
def main():
    global exchange
    if 'exchange' not in globals() or exchange is None:
        Log("❌ 未检测到交易所", "#FF0000")
        return
    Log("🚀 基于挂单的策略启动", "#00FF00")
    exchange.SetContractType("swap")

    # 初始化策略管理器 (直接使用ext对象中的工具类)
    strategy = OrderBasedStrategyManager(exchange, STRATEGY_CONFIG)
    # UI按钮配置
    btn_trade = {
        "type": "button",
        "cmd": "TradeCmd",
        "name": "🚀 开仓",
        "group": [
            {"name": "symbol", "type": "selected", "defValue": 0, "options": MY_SYMBOLS, "description": "交易币种"},
            {"name": "direction", "type": "selected", "defValue": "buy|sell", "description": "方向"},
            {"name": "mode", "type": "selected", "defValue": "1.市价|2.限价|3.市价激活跟踪|4.限价激活跟踪", "description": "入场模式"},
            {"name": "volatility", "type": "selected", "defValue": "小波动|中波动|大波动", "description": "波动模式"},
            {"name": "max_loss", "type": "number", "defValue": 50, "description": "最大亏损(USDT)"},
            {"name": "atr_percentage", "type": "number", "defValue": 0, "description": "ATR百分比(0=默认周期20)"},
            {"name": "limit_price", "type": "number", "defValue": 0, "description": "触发价(限价模式)"}
        ]
    }
    btn_confirm = {"type": "button", "cmd": "ConfirmEntry", "name": "✅ 确认开仓"}
    btn_cancel = {"type": "button", "cmd": "CancelEntry", "name": "❌ 取消"}
    btn_reset = {"type": "button", "cmd": "ResetStrategy", "name": "🔄 重置策略"}
    btn_info = {"type": "button", "cmd": "ShowInfo", "name": "📊 查看状态"}
    ui_layout = (
        f'`{json.dumps(btn_trade, ensure_ascii=False)}`\n' +
        f'`{json.dumps(btn_confirm, ensure_ascii=False)}`\n' +
        f'`{json.dumps(btn_cancel, ensure_ascii=False)}`\n' +
        f'`{json.dumps(btn_reset, ensure_ascii=False)}`\n' +
        f'`{json.dumps(btn_info, ensure_ascii=False)}`'
    )
    status_display = "等待操作..."
    # 主循环
    while True:
        try:
            # 定期检查仓位变化
            strategy.check_position_and_update_state()
            # UI渲染
            if strategy.state == "WAIT_CONFIRM":
                confirm_info = strategy.get_confirm_info()
                status_display = "\n".join(confirm_info)
            else:
                status_display = strategy.get_status_info()
            LogStatus(f"{ui_layout}\n\n最后更新: {_D()}\n\n{status_display}")
            # 处理命令
            cmd = GetCommand()
            if cmd:
                try:
                    if cmd.startswith("TradeCmd:"):
                        data = json.loads(cmd.split(":", 1)[1])
                        symbol = MY_SYMBOLS[int(data['symbol'])]
                        direction = "buy" if int(data['direction']) == 0 else "sell"
                        mode = int(data['mode']) + 1
                        volatility_mode = int(data.get('volatility', 0))  # 0=小波动, 1=中波动, 2=大波动
                        max_loss = float(data['max_loss'])
                        atr_percentage = float(data.get('atr_percentage', 0))  # 0表示使用默认周期
                        limit_price = float(data.get('limit_price', 0))
                        strategy.start_entry(symbol, direction, max_loss, mode, limit_price, volatility_mode, atr_percentage)
                    elif cmd == "ConfirmEntry":
                        strategy.confirm_entry()
                    elif cmd == "CancelEntry":
                        strategy.cancel_entry()
                    elif cmd == "ResetStrategy":
                        strategy._reset()
                    elif cmd == "ShowInfo":
                        status_display = strategy.get_status_info()
                except Exception as e:
                    Log(f"❌ 指令处理错误: {e}", "#FF0000")
        except Exception as e:
            Log(f"❌ 主循环错误: {e}", "#FF0000")
        Sleep(2000)  # 每2秒循环一次

# 启动主程序
if __name__ == "__main__":
    main()
