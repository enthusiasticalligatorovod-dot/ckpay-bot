import logging
import json
import os
import re
import asyncio
from datetime import datetime, time, timedelta
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    ChatMemberHandler,
    filters
)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

DATA_FILE = "ledger.json"

# ----------------- 🔑 核心权限设置 -----------------
OWNER_USERNAME = "@CKpay11" 
# 仅保留主管理员账号，其余账号必须在群内通过指令添加
ALLOWED_ADMINS = [OWNER_USERNAME.lower()] 

default_data = {
    "rates": {},          # key: chat_id, value: float (群组独立汇率)
    "cut_off_hours": {},  # key: chat_id, value: int (群组独立日切)
    "records": {},        # key: chat_id, value: list of records
    "start_time": {},     # key: chat_id, value: timestamp
    "active_chats": [],   # 已激活群组列表
    "group_admins": {},   # key: chat_id, value: list of group admins (群组独立操作人)
    "admins": ALLOWED_ADMINS, # 全局固定超级管理员
}

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "rates" not in data:
                    data["rates"] = {}
                if "cut_off_hours" not in data:
                    data["cut_off_hours"] = {}
                if "active_chats" not in data:
                    data["active_chats"] = []
                if "group_admins" not in data:
                    data["group_admins"] = {}
                data["admins"] = ALLOWED_ADMINS
                return data
        except Exception as e:
            print(f"读取数据文件失败: {e}")
    return default_data

def save_data(data):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"保存数据文件失败: {e}")

app_data = load_data()

def get_chat_rate(chat_id):
    return float(app_data.get("rates", {}).get(chat_id, 165.0))

def get_chat_cutoff(chat_id):
    return int(app_data.get("cut_off_hours", {}).get(chat_id, 1))

def is_authorized(user, chat_id=None):
    """严密校验用户操作权限（主管理员全局有效，普通操作人按群独立）"""
    if not user:
        return False
    username = f"@{user.username.lower().strip()}" if user.username else None
    user_id = str(user.id).strip()
    
    # 1. 检查是否为主管理员
    global_admins = [str(a).lower().strip() for a in ALLOWED_ADMINS]
    if username and username in global_admins:
        return True
    if user_id in global_admins:
        return True
        
    # 2. 检查是否为本群组独立授权的操作人
    if chat_id:
        chat_id_str = str(chat_id)
        grp_admins = [str(a).lower().strip() for a in app_data.get("group_admins", {}).get(chat_id_str, [])]
        if username and username in grp_admins:
            return True
        if user_id in grp_admins:
            return True
            
    return False

# ----------------- 🛡️ 自动防非授权拉群控制与退群清理 -----------------
async def track_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """检测机器人加入/离开群组事件"""
    result = update.my_chat_member
    if not result:
        return

    actor = result.from_user
    new_status = result.new_chat_member.status
    chat_id = str(result.chat.id)
    actor_username = f"@{actor.username}".lower() if actor.username else ""

    # 机器人被剔除或主动退群：清空该群的操作人权限和数据
    if new_status in ["left", "kicked"]:
        if chat_id in app_data.get("active_chats", []):
            app_data["active_chats"].remove(chat_id)
        if "group_admins" in app_data and chat_id in app_data["group_admins"]:
            app_data["group_admins"][chat_id] = []
        app_data.get("records", {})[chat_id] = []
        save_data(app_data)
        print(f"🗑️ 机器人离开/被踢出群组 {chat_id}，已自动清空该群组的所有操作人权限与账单。")
        return

    # 机器人被拉入群组
    if new_status in ["member", "administrator"]:
        print(f"🔍 机器人被加入群组 {chat_id}，操作人: {actor.first_name} ({actor_username} / ID:{actor.id})")
        
        owner_clean = OWNER_USERNAME.lower().strip()
        is_owner = (actor_username == owner_clean) or (str(actor.id) == owner_clean)
        
        if not is_owner:
            print(f"🚫 非授权用户 {actor_username}(ID:{actor.id}) 尝试将机器人加入群组，正在自动退群...")
            try:
                await context.bot.send_message(
                    chat_id=chat_id, 
                    text="⚠️ 权限不足：非授权管理员无权将本机器人加入群组，机器人即将自动退出。"
                )
                await context.bot.leave_chat(chat_id)
            except Exception as e:
                print(f"退群失败: {e}")
        else:
            # 重新被主管理员拉入时：清空该群组之前添加的所有操作人权限及记录
            if "group_admins" not in app_data:
                app_data["group_admins"] = {}
            app_data["group_admins"][chat_id] = []
            
            if chat_id in app_data.get("active_chats", []):
                app_data["active_chats"].remove(chat_id)
                
            app_data["records"][chat_id] = []
            app_data["start_time"][chat_id] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_data(app_data)
            print(f"✅ 授权管理员 {actor_username} 重新拉入机器人到群组 {chat_id}，已重置操作人权限与等待状态。")
            
            try:
                await context.bot.send_message(
                    chat_id=chat_id, 
                    text="感谢拉我入群，祝老板多赚UU"
                )
            except Exception as e:
                print(f"发送入群欢迎词失败: {e}")

# ----------------- 网页账单渲染器 -----------------
async def handle_web_bill(request):
    chat_id = request.query.get("chatid", "")
    records = app_data.get("records", {}).get(chat_id, [])
    current_rate = get_chat_rate(chat_id)

    rows_html = ""
    for r in records:
        cny = r.get('amount_cny', 0)
        usdt = r.get('amount_usdt', 0)
        amt_str = f"{int(cny)}" if isinstance(cny, float) and cny.is_integer() else str(cny)
        rate_val = r.get('rate_snapshot', current_rate)
        rate_str = f"{int(rate_val)}" if isinstance(rate_val, float) and rate_val.is_integer() else str(rate_val)
        
        rows_html += f"""
        <tr>
            <td style="padding:12px; border-bottom:1px solid #eee;">{r.get('time', '')}</td>
            <td style="padding:12px; border-bottom:1px solid #eee;">
                <strong>{amt_str}</strong><br>
                <span style="color:#0969da; font-size:12px;">₮ {usdt:.2f}({rate_str})</span>
            </td>
            <td style="padding:12px; border-bottom:1px solid #eee;">{r.get('user', '')}</td>
            <td style="padding:12px; border-bottom:1px solid #eee;">CKpay-元宝</td>
        </tr>
        """

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Bill</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 20px; background: #fff; color: #333; }}
            .filter-box {{ display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }}
            input, select, button {{ padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }}
            button {{ background: #409eff; color: white; border: none; cursor: pointer; }}
            table {{ width: 100%; border-collapse: collapse; text-align: left; }}
            th {{ background: #fafafa; padding: 12px; font-weight: 600; color: #666; border-bottom: 2px solid #eee; }}
            .title {{ font-size: 18px; font-weight: bold; margin-bottom: 15px; color: #2c3e50; }}
        </style>
    </head>
    <body>
        <div class="filter-box">
            <input type="text" value="{datetime.now().strftime('%Y-%m-%d')} 06:00:00" style="width:180px;">
            <span>-</span>
            <input type="text" value="{datetime.now().strftime('%Y-%m-%d')} 06:00:00" style="width:180px;">
        </div>
        <div class="filter-box">
            <select style="width: 200px;"><option>当日第1笔账单</option></select>
        </div>
        <div class="filter-box">
            <input type="text" placeholder="请输入您要查询的名字" style="flex:1;">
            <button>查询</button>
        </div>

        <div class="title">入款 ({len(records)}笔)</div>

        <table>
            <thead>
                <tr>
                    <th>时间</th>
                    <th>金额</th>
                    <th>回复人</th>
                    <th>操作人</th>
                </tr>
            </thead>
            <tbody>
                {rows_html if rows_html else '<tr><td colspan="4" style="text-align:center;padding:20px;color:#999;">暂无数据</td></tr>'}
            </tbody>
        </table>
    </body>
    </html>
    """
    return web.Response(text=html_content, content_type='text/html')

# ----------------- 报表面板生成 -----------------
def generate_ledger_report(chat_id):
    try:
        records_dict = app_data.get("records", {})
        records = records_dict.get(chat_id, [])
        current_rate = get_chat_rate(chat_id)
        
        inbound_records = [r for r in records if r.get("type") == "收入"]
        payout_records = [r for r in records if r.get("type") == "支出"]
        
        msg = f"今日入款 （{len(inbound_records)}笔）\n"
        if not inbound_records:
            msg += "（暂无入款记录）\n"
        else:
            for r in inbound_records:
                cny = r.get('amount_cny', 0)
                usdt = r.get('amount_usdt', 0)
                amt_str = f"{int(cny)}" if isinstance(cny, float) and cny.is_integer() else str(cny)
                rate_val = r.get('rate_snapshot', current_rate)
                rate_str = f"{int(rate_val)}" if isinstance(rate_val, float) and rate_val.is_integer() else str(rate_val)
                msg += f"{r.get('time', '')}  {amt_str} / {rate_str}={usdt:.2f}U  {r.get('user', '')}\n"
                
        msg += "\n"
        msg += f"今日下发 （{len(payout_records)}笔）\n"
        if not payout_records:
            msg += "（暂无下发记录）\n"
        else:
            for r in payout_records:
                cny = r.get('amount_cny', 0)
                usdt = r.get('amount_usdt', 0)
                amt_str = f"{int(cny)}" if isinstance(cny, float) and cny.is_integer() else str(cny)
                rate_val = r.get('rate_snapshot', current_rate)
                rate_str = f"{int(rate_val)}" if isinstance(rate_val, float) and rate_val.is_integer() else str(rate_val)
                msg += f"{r.get('time', '')}  {amt_str} / {rate_str}={usdt:.2f}U  {r.get('user', '')}\n"
                
        total_inbound_cny = sum(r.get('amount_cny', 0) for r in inbound_records)
        total_inbound_usdt = sum(r.get('amount_usdt', 0) for r in inbound_records)
        
        total_payout_cny = sum(r.get('amount_cny', 0) for r in payout_records)
        total_payout_usdt = sum(r.get('amount_usdt', 0) for r in payout_records)
        
        unpaid_cny = total_inbound_cny - total_payout_cny
        unpaid_usdt = total_inbound_usdt - total_payout_usdt
        
        in_cny_str = f"{int(total_inbound_cny)}" if isinstance(total_inbound_cny, float) and total_inbound_cny.is_integer() else str(total_inbound_cny)
        out_cny_str = f"{int(total_payout_cny)}" if isinstance(total_payout_cny, float) and total_payout_cny.is_integer() else str(total_payout_cny)
        un_cny_str = f"{int(unpaid_cny)}" if isinstance(unpaid_cny, float) and unpaid_cny.is_integer() else str(unpaid_cny)
        
        msg += f"\n总入款金额：{in_cny_str}\n\n"
        msg += f"应下发：{in_cny_str} | {total_inbound_usdt:.2f}U\n"
        msg += f"已下发：{out_cny_str} | {total_payout_usdt:.2f}U\n"
        msg += f"未下发：{un_cny_str} | {unpaid_usdt:.2f}U"
        return msg
    except Exception as e:
        return f"生成报表错误: {e}"

def generate_finished_report(chat_id):
    records_dict = app_data.get("records", {})
    records = records_dict.get(chat_id, [])
    current_rate = get_chat_rate(chat_id)
    
    inbound_records = [r for r in records if r.get("type") == "收入"]
    payout_records = [r for r in records if r.get("type") == "支出"]
    
    msg = "本次账单全部完结\n"
    msg += f"入款：（{len(inbound_records)}笔）\n"
    if not inbound_records:
        msg += "（无入款）\n"
    else:
        for r in inbound_records:
            cny = r.get('amount_cny', 0)
            usdt = r.get('amount_usdt', 0)
            amt_str = f"{int(cny)}" if isinstance(cny, float) and cny.is_integer() else str(cny)
            rate_val = r.get('rate_snapshot', current_rate)
            rate_str = f"{int(rate_val)}" if isinstance(rate_val, float) and rate_val.is_integer() else str(rate_val)
            msg += f"  {r.get('time', '')}  {amt_str} / {rate_str} = {usdt:.2f}U\n"
            
    msg += f"\n下发：（{len(payout_records)}笔）\n"
    if not payout_records:
        msg += "（无下发）\n"
    else:
        for r in payout_records:
            cny = r.get('amount_cny', 0)
            usdt = r.get('amount_usdt', 0)
            amt_str = f"{int(cny)}" if isinstance(cny, float) and cny.is_integer() else str(cny)
            msg += f"  {r.get('time', '')}  {usdt:.2f}U ({amt_str})\n"
            
    total_inbound_cny = sum(r.get('amount_cny', 0) for r in inbound_records)
    total_inbound_usdt = sum(r.get('amount_usdt', 0) for r in inbound_records)
    total_payout_usdt = sum(r.get('amount_usdt', 0) for r in payout_records)
    unpaid_usdt = total_inbound_usdt - total_payout_usdt
    
    in_cny_str = f"{int(total_inbound_cny)}" if isinstance(total_inbound_cny, float) and total_inbound_cny.is_integer() else str(total_inbound_cny)
    rate_str = f"{int(current_rate)}" if current_rate.is_integer() else str(current_rate)

    msg += f"\n总入款：{in_cny_str}\n"
    msg += f"总入U数：{total_inbound_usdt:.2f}\n\n"
    msg += f"USD汇率：{rate_str}\n\n"
    msg += f"应下发：{total_inbound_usdt:.2f} USDT\n"
    msg += f"总下发：{total_payout_usdt:.2f} USDT\n"
    msg += f"剩余：{unpaid_usdt:.2f} USDT\n\n"

    start_ts_str = app_data.get("start_time", {}).get(chat_id)
    if start_ts_str:
        start_dt = datetime.strptime(start_ts_str, "%Y-%m-%d %H:%M:%S")
        mins = int((datetime.now() - start_dt).total_seconds() / 60)
    else:
        mins = 0
    msg += f"已工作{mins}分钟"
    return msg

def get_ledger_keyboard(chat_id):
    today = datetime.now().strftime("%Y-%m-%d")
    server_host = os.environ.get("SERVER_HOST", "http://127.0.0.1:8080")
    web_bill_url = f"{server_host}/bill?chatid={chat_id}&date={today}%2006:00:00"
    keyboard = [
        [InlineKeyboardButton("📒 完整账单", url=web_bill_url)]
    ]
    return InlineKeyboardMarkup(keyboard)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    user = update.effective_user

    # 判断是否属于机器人相关指令
    is_cmd = (
        text in ["开始", "开始记账", "激活"] or
        text.startswith("+") or text.startswith("＋") or
        "下发" in text or "日切" in text or "汇率" in text or
        text in ["结束", "结束账单", "结算", "完结", "删除账单", "清空账单", "撤回", "恢复"] or
        "设置操作人" in text or "添加操作人" in text or "操作人" in text
    )

    if not is_cmd:
        return  # 普通闲聊消息直接忽略

    # 🛑 第一关卡：绝对严格的权限拦截
    if not is_authorized(user, chat_id):
        await update.message.reply_text("⚠️ 权限不足，请先联系管理员添加操作人权限。")
        return

    # 1. 响应“开始”激活指令（仅限已授权人员）
    if text in ["开始", "开始记账", "激活"]:
        if chat_id not in app_data.get("active_chats", []):
            app_data["active_chats"].append(chat_id)
            save_data(app_data)
        report = generate_ledger_report(chat_id)
        await update.message.reply_text(report, reply_markup=get_ledger_keyboard(chat_id))
        return

    is_active = chat_id in app_data.get("active_chats", [])
    records = app_data.get("records", {}).get(chat_id, [])

    # 2. 响应“结束”/“结算”指令（未开始或无记录时提示“还没有开始，何谈结束。”）
    if text in ["结束", "结束账单", "结算", "完结"]:
        if not is_active or not records:
            await update.message.reply_text("还没有开始，何谈结束。")
            return
        
        report = generate_finished_report(chat_id)
        await update.message.reply_text(report, reply_markup=get_ledger_keyboard(chat_id))
        app_data["records"][chat_id] = []
        app_data["start_time"][chat_id] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if chat_id in app_data["active_chats"]:
            app_data["active_chats"].remove(chat_id)
        save_data(app_data)
        return

    # 3. 未开始记账前尝试记账/删除/撤回：提示“请开始记账”
    if not is_active:
        if (text.startswith("+") or text.startswith("＋") or "下发" in text or 
            "删除账单" in text or "清空账单" in text or "撤回" in text or "恢复" in text):
            await update.message.reply_text("请开始记账")
            return

    operator_name = f"@{user.username}" if user.username else user.first_name
    short_time = datetime.now().strftime("%H:%M:%S")

    if "records" not in app_data:
        app_data["records"] = {}
    if chat_id not in app_data["records"]:
        app_data["records"][chat_id] = []
    if "start_time" not in app_data:
        app_data["start_time"] = {}
    if chat_id not in app_data["start_time"]:
        app_data["start_time"][chat_id] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    current_rate = get_chat_rate(chat_id)

    # 4. 设置日切
    if "日切" in text:
        match = re.search(r'([\d]{1,2})', text)
        if match:
            hour = int(match.group(1))
            if 0 <= hour <= 23:
                if "cut_off_hours" not in app_data:
                    app_data["cut_off_hours"] = {}
                app_data["cut_off_hours"][chat_id] = hour
                save_data(app_data)
                await update.message.reply_text(f"✅ 当前群组日切时间已单独更新为：每天 {hour:02d}:00")
            else:
                await update.message.reply_text("⚠️ 小时必须在 0 到 23 之间。")
        else:
            await update.message.reply_text("⚠️ 格式错误，请使用：日切1 或 日切2")
        return

    # 5. 添加本群专属操作人
    if "设置操作人" in text or "添加操作人" in text or "操作人" in text:
        match = re.search(r'(@[\w\d_]+)', text)
        if match:
            new_admin = match.group(1).lower()
            if "group_admins" not in app_data:
                app_data["group_admins"] = {}
            if chat_id not in app_data["group_admins"]:
                app_data["group_admins"][chat_id] = []
            if new_admin not in app_data["group_admins"][chat_id]:
                app_data["group_admins"][chat_id].append(new_admin)
                save_data(app_data)
            await update.message.reply_text(f"✅ 成功为此群组添加授权操作人：{new_admin}")
        else:
            await update.message.reply_text("⚠️ 格式错误，请使用：添加操作人@用户名")
        return

    # 6. 清空/删除账单
    if "删除账单" in text or "清空账单" in text:
        app_data["records"][chat_id] = []
        app_data["start_time"][chat_id] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_data(app_data)
        await update.message.reply_text("🗑️ 当前账单已成功清空重置！")
        report = generate_ledger_report(chat_id)
        await update.message.reply_text(report, reply_markup=get_ledger_keyboard(chat_id))
        return

    # 7. 撤回上一笔
    if "撤回" in text or "恢复" in text:
        records_list = app_data["records"].get(chat_id, [])
        if not records_list:
            await update.message.reply_text("当前没有任何可撤销的账目记录。")
            return
        removed = records_list.pop()
        save_data(app_data)
        
        await update.message.reply_text(f"⚠️ 已成功撤销上一笔记录：{removed.get('type')} {removed.get('amount_cny')}")
        report = generate_ledger_report(chat_id)
        await update.message.reply_text(report, reply_markup=get_ledger_keyboard(chat_id))
        return

    # 8. 设置汇率
    if "汇率" in text:
        match = re.search(r'([\d\.]+)', text)
        if match:
            new_rate = float(match.group(1))
            if "rates" not in app_data:
                app_data["rates"] = {}
            app_data["rates"][chat_id] = new_rate
            save_data(app_data)
            await update.message.reply_text(f"✅ 当前群组汇率已单独更新为：{new_rate}")
        return

    # 9. 入款（加账）
    if text.startswith("+") or text.startswith("＋"):
        clean_text = text.replace("u", "").replace("U", "").replace("+", "").replace("＋", "").strip()
        match = re.search(r'([\d\.]+)', clean_text)
        if match:
            amount_cny = float(match.group(1))
            amount_usdt = amount_cny / current_rate if current_rate > 0 else 0

            record = {
                "time": short_time,
                "user": operator_name,
                "type": "收入",
                "amount_cny": amount_cny,
                "amount_usdt": amount_usdt,
                "rate_snapshot": current_rate
            }
            app_data["records"][chat_id].append(record)
            save_data(app_data)

            report = generate_ledger_report(chat_id)
            await update.message.reply_text(report, reply_markup=get_ledger_keyboard(chat_id))
        return

    # 10. 下发（出款）
    if "下发" in text:
        clean_text = text.replace("u", "").replace("U", "").replace("-", "").replace("下发", "").strip()
        match = re.search(r'([\d\.]+)', clean_text)
        if match:
            amount_usdt = float(match.group(1))
            amount_cny = amount_usdt * current_rate

            record = {
                "time": short_time,
                "user": operator_name,
                "type": "支出",
                "amount_cny": amount_cny,
                "amount_usdt": amount_usdt,
                "rate_snapshot": current_rate
            }
            app_data["records"][chat_id].append(record)
            save_data(app_data)

            report = generate_ledger_report(chat_id)
            await update.message.reply_text(report, reply_markup=get_ledger_keyboard(chat_id))
        return

# ----------------- 各群独立日切自动结算定时任务 -----------------
async def daily_auto_finish_task(bot):
    while True:
        await asyncio.sleep(30)
        now = datetime.now()
        chats = list(app_data.get("records", {}).keys())
        
        for chat_id in chats:
            if chat_id in app_data.get("active_chats", []):
                cut_hour = get_chat_cutoff(chat_id)
                if now.hour == cut_hour and now.minute == 0 and now.second < 30:
                    records = app_data["records"].get(chat_id, [])
                    if records:
                        try:
                            report = generate_finished_report(chat_id)
                            await bot.send_message(chat_id=chat_id, text=report, reply_markup=get_ledger_keyboard(chat_id))
                            app_data["records"][chat_id] = []
                            app_data["start_time"][chat_id] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            if chat_id in app_data["active_chats"]:
                                app_data["active_chats"].remove(chat_id)
                            save_data(app_data)
                        except Exception as e:
                            print(f"发送群 {chat_id} 日切自动结算失败: {e}")

async def main():
    # 启动 aiohttp 网页服务（适配云端动态端口）
    web_app = web.Application()
    web_app.router.add_get('/bill', handle_web_bill)
    runner = web.AppRunner(web_app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 网页账单服务已启动 (端口 {port})")

    TOKEN = "9123032276:AAF_J4trsLhrC1mByepaaVx9zVCgIAFTBtE"
    print("正在连接 Telegram 服务器并初始化机器人...")
    app = ApplicationBuilder().token(TOKEN).build()
    
    app.add_handler(ChatMemberHandler(track_chats, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", handle_message))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    print("🤖 CKpay 记账机器人已成功启动！")
    await app.initialize()
    await app.start()
    
    # 启动后台日切任务
    asyncio.create_task(daily_auto_finish_task(app.bot))
    
    # 启动长轮询监听消息
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    
    # 保持事件循环持续运行
    try:
        await asyncio.Event().wait()
    finally:
        await app.stop()
        await app.shutdown()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\n❌ 发生错误：{e}")