import asyncio
import logging
import aiosqlite
from datetime import datetime
from contextlib import suppress
from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = "7439318234:AAEa9uF3-OAbVBj6xX7ODOd6vjSIZb48WFQ"
ADMIN_ID = 710787759
DB_NAME = "cash_calc.db"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)


# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, full_name TEXT)')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE, 
                owner_id INTEGER
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER,
                user_id INTEGER,
                full_name TEXT,
                balance INTEGER DEFAULT 0,
                is_set INTEGER DEFAULT 0,
                UNIQUE(group_id, user_id)
            )
        ''')

        await db.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id INTEGER,
                amount INTEGER,
                operation_type TEXT,
                created_at TEXT
            )
        ''')
        await db.commit()


# --- ФУНКЦИИ БД ---

async def upsert_user(user_id, full_name):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            INSERT INTO users (user_id, full_name) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name
        ''', (user_id, full_name))
        await db.commit()


async def get_user_name(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT full_name FROM users WHERE user_id = ?', (user_id,)) as cursor:
            res = await cursor.fetchone()
            return res[0] if res else "Неизвестный"


async def get_group_by_name(name):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM groups WHERE name = ?', (name,)) as cursor:
            return await cursor.fetchone()


async def get_all_groups_for_admin():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM groups ORDER BY name') as cursor:
            return await cursor.fetchall()


async def delete_group_totally(group_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('DELETE FROM transactions WHERE member_id IN (SELECT id FROM members WHERE group_id = ?)',
                         (group_id,))
        await db.execute('DELETE FROM members WHERE group_id = ?', (group_id,))
        await db.execute('DELETE FROM groups WHERE id = ?', (group_id,))
        await db.commit()


async def create_group(name, owner_id):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute('INSERT INTO groups (name, owner_id) VALUES (?, ?)', (name, owner_id))
        group_id = cursor.lastrowid
        await db.commit()
        return group_id


async def add_member(group_id, user_id, full_name):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute('SELECT id FROM members WHERE group_id = ? AND user_id = ?', (group_id, user_id))
        if await cursor.fetchone():
            return None
        await db.execute('INSERT INTO members (group_id, user_id, full_name) VALUES (?, ?, ?)',
                         (group_id, user_id, full_name))
        await db.commit()
        return True


async def get_user_groups(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        sql = '''
            SELECT g.id, g.name 
            FROM groups g
            JOIN members m ON g.id = m.group_id
            WHERE m.user_id = ?
        '''
        async with db.execute(sql, (user_id,)) as cursor:
            return await cursor.fetchall()


async def get_group_members(group_id):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM members WHERE group_id = ?', (group_id,)) as cursor:
            return await cursor.fetchall()


async def get_member_details(member_id):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        sql = '''
            SELECT m.*, g.name as group_name
            FROM members m
            JOIN groups g ON m.group_id = g.id
            WHERE m.id = ?
        '''
        async with db.execute(sql, (member_id,)) as cursor:
            return await cursor.fetchone()


async def update_balance(member_id, amount, operation):
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    async with aiosqlite.connect(DB_NAME) as db:
        if operation == 'set':
            await db.execute('UPDATE members SET balance = ?, is_set = 1 WHERE id = ?', (amount, member_id))
            await db.execute(
                'INSERT INTO transactions (member_id, amount, operation_type, created_at) VALUES (?, ?, ?, ?)',
                (member_id, amount, 'Начальный взнос', now))
        elif operation == 'withdraw':
            await db.execute('UPDATE members SET balance = balance - ? WHERE id = ?', (amount, member_id))
            await db.execute(
                'INSERT INTO transactions (member_id, amount, operation_type, created_at) VALUES (?, ?, ?, ?)',
                (member_id, amount, 'Снятие', now))
        await db.commit()


async def get_transactions(member_id):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM transactions WHERE member_id = ? ORDER BY id DESC LIMIT 10',
                              (member_id,)) as cursor:
            return await cursor.fetchall()


# --- FSM ---
class CreatePointState(StatesGroup):
    waiting_for_name = State()


class SetBalanceState(StatesGroup):
    waiting_for_amount = State()


class WithdrawState(StatesGroup):
    waiting_for_amount = State()


# --- КЛАВИАТУРЫ ---

def get_start_menu_kb(user_id):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💰 Наличка (Аванс / ЗП)", callback_data="cash_section_menu"))
    if user_id == ADMIN_ID:
        builder.row(InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel_start"))
    return builder.as_markup()


# НОВАЯ: Клавиатура МЕНЮ админа (первый слой)
def get_admin_main_menu_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📂 Управление точками (зп / аванс)", callback_data="admin_manage_points"))
    builder.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data="main_menu_start"))
    return builder.as_markup()


# ОБНОВЛЕННАЯ: Список точек (второй слой)
async def get_admin_points_kb():
    builder = InlineKeyboardBuilder()
    groups = await get_all_groups_for_admin()
    for g in groups:
        builder.row(InlineKeyboardButton(text=f"🗑 {g['name']}", callback_data=f"admin_ask_del_{g['id']}"))
    # Кнопка назад теперь ведет в МЕНЮ АДМИНА, а не в главное
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel_start"))
    return builder.as_markup()


async def get_points_list_kb(user_id):
    builder = InlineKeyboardBuilder()
    groups = await get_user_groups(user_id)
    for g in groups:
        builder.row(InlineKeyboardButton(text=f"📂 {g['name']}", callback_data=f"open_group_{g['id']}"))
    builder.row(InlineKeyboardButton(text="➕ Создать/Найти точку", callback_data="create_point"))
    builder.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data="main_menu_start"))
    return builder.as_markup()


async def get_group_members_kb(group_id):
    builder = InlineKeyboardBuilder()
    members = await get_group_members(group_id)
    for m in members:
        builder.row(InlineKeyboardButton(text=f"👤 {m['full_name']} ({m['balance']} р.)",
                                         callback_data=f"view_member_{m['id']}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад к точкам", callback_data="cash_section_menu"))
    return builder.as_markup()


def get_member_menu_kb(member_id, group_id, is_set, balance):
    builder = InlineKeyboardBuilder()
    if is_set == 0:
        builder.row(InlineKeyboardButton(text="➕ Начислить наличку", callback_data=f"set_balance_{member_id}"))
    else:
        if balance > 0:
            builder.row(InlineKeyboardButton(text="💸 Забрать наличку", callback_data=f"withdraw_{member_id}"))

    builder.row(InlineKeyboardButton(text="📜 История налички", callback_data=f"history_{member_id}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад к сотрудникам", callback_data=f"open_group_{group_id}"))
    return builder.as_markup()


def get_admin_confirm_del_kb(group_id):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Да, удалить навсегда", callback_data=f"admin_confirm_del_{group_id}"))
    builder.row(InlineKeyboardButton(text="❌ Нет, отмена", callback_data="admin_manage_points"))  # Возврат к списку
    return builder.as_markup()


def get_cancel_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cash_section_menu")]])


def get_approval_kb(requester_id, requester_name, group_name, group_id):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Принять", callback_data=f"approve_{requester_id}_{group_id}"))
    builder.row(InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{requester_id}"))
    return builder.as_markup()


# --- ЛОГИКА ---

@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await upsert_user(message.from_user.id, message.from_user.full_name)
    kb = get_start_menu_kb(message.from_user.id)
    await message.answer("<b>⚡️ Главное меню</b>", reply_markup=kb, parse_mode="HTML")
    with suppress(TelegramBadRequest): await message.delete()


@router.callback_query(F.data == "main_menu_start")
async def back_to_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = get_start_menu_kb(callback.from_user.id)
    await callback.message.edit_text("<b>⚡️ Главное меню</b>", reply_markup=kb, parse_mode="HTML")


# --- ЛОГИКА АДМИНА (ОБНОВЛЕННАЯ) ---

# 1. Открываем само меню админа
@router.callback_query(F.data == "admin_panel_start")
async def open_admin_panel(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    kb = get_admin_main_menu_kb()
    await callback.message.edit_text("<b>⚙️ Админ-панель</b>\nВыберите действие:", reply_markup=kb, parse_mode="HTML")


# 2. Нажимаем "Управление точками" -> Показываем список
@router.callback_query(F.data == "admin_manage_points")
async def admin_list_points(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    kb = await get_admin_points_kb()
    await callback.message.edit_text("<b>📂 Управление точками</b>\n\nНажмите на точку, чтобы <b>УДАЛИТЬ</b> её.",
                                     reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_ask_del_"))
async def admin_ask_delete(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    group_id = int(callback.data.split("_")[3])
    await callback.message.edit_text(
        "⚠️ <b>Вы уверены?</b>\nЭто удалит точку, всех сотрудников и всю историю операций безвозвратно.",
        reply_markup=get_admin_confirm_del_kb(group_id),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("admin_confirm_del_"))
async def admin_confirm_delete(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    group_id = int(callback.data.split("_")[3])
    await delete_group_totally(group_id)
    kb = await get_admin_points_kb()
    await callback.message.edit_text("✅ <b>Точка успешно удалена.</b>", reply_markup=kb, parse_mode="HTML")


# --- РАЗДЕЛ: НАЛИЧКА ---

@router.callback_query(F.data == "cash_section_menu")
async def open_cash_section(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = await get_points_list_kb(callback.from_user.id)
    await callback.message.edit_text("<b>💰 Ваши точки:</b>", reply_markup=kb, parse_mode="HTML")


# --- СОЗДАНИЕ / ПОИСК ТОЧКИ ---

@router.callback_query(F.data == "create_point")
async def start_create_point(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Введите <b>название точки</b>:",
        reply_markup=get_cancel_kb(), parse_mode="HTML"
    )
    await state.set_state(CreatePointState.waiting_for_name)


@router.message(CreatePointState.waiting_for_name)
async def process_point_name(message: types.Message, state: FSMContext):
    await upsert_user(message.from_user.id, message.from_user.full_name)
    point_name = message.text.strip()
    user_id = message.from_user.id
    full_name = message.from_user.full_name

    with suppress(TelegramBadRequest):
        await message.delete()

    existing_group = await get_group_by_name(point_name)

    if existing_group:
        owner_id = existing_group['owner_id']
        group_id = existing_group['id']

        if owner_id == user_id:
            await message.answer("⚠️ Вы уже являетесь владельцем этой точки.", reply_markup=get_cancel_kb())
            return

        try:
            await bot.send_message(
                owner_id,
                f"🔔 <b>Запрос на добавление!</b>\n\n"
                f"Пользователь <b>{full_name}</b> хочет присоединиться к точке <b>{existing_group['name']}</b>.",
                reply_markup=get_approval_kb(user_id, full_name, existing_group['name'], group_id),
                parse_mode="HTML"
            )
            await message.answer(f"⏳ Точка <b>{point_name}</b> уже существует.\nВладельцу отправлен запрос.",
                                 reply_markup=get_cancel_kb(), parse_mode="HTML")
        except TelegramForbiddenError:
            await message.answer("❌ Владелец точки заблокировал бота.", reply_markup=get_cancel_kb())
    else:
        group_id = await create_group(point_name, user_id)
        await add_member(group_id, user_id, full_name)
        await state.clear()
        kb = await get_group_members_kb(group_id)
        await message.answer(f"✅ Точка <b>{point_name}</b> создана!", reply_markup=kb, parse_mode="HTML")


# --- ОДОБРЕНИЕ ЗАЯВОК ---

@router.callback_query(F.data.startswith("approve_"))
async def approve_user(callback: CallbackQuery):
    parts = callback.data.split("_")
    target_user_id = int(parts[1])
    group_id = int(parts[2])

    real_name = await get_user_name(target_user_id)

    res = await add_member(group_id, target_user_id, real_name)

    if res:
        await callback.message.edit_text(f"✅ <b>{real_name} принят(а).</b>", parse_mode="HTML")
        try:
            kb_for_new_member = await get_group_members_kb(group_id)
            await bot.send_message(
                target_user_id,
                "🥳 <b>Вас приняли в точку!</b>\nТеперь вы можете управлять наличностью.",
                reply_markup=kb_for_new_member,
                parse_mode="HTML"
            )
        except:
            pass
    else:
        await callback.message.edit_text("⚠️ Пользователь уже добавлен.", parse_mode="HTML")


@router.callback_query(F.data.startswith("reject_"))
async def reject_user(callback: CallbackQuery):
    target_user_id = int(callback.data.split("_")[1])
    await callback.message.edit_text("❌ <b>Заявка отклонена.</b>", parse_mode="HTML")
    try:
        await bot.send_message(target_user_id, "😔 Владелец отклонил запрос.", parse_mode="HTML")
    except:
        pass


# --- НАВИГАЦИЯ ВНУТРИ ТОЧКИ ---

@router.callback_query(F.data.startswith("open_group_"))
async def open_group(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[2])
    kb = await get_group_members_kb(group_id)
    await callback.message.edit_text("👥 <b>Сотрудники точки:</b>", reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("view_member_"))
async def view_member(callback: CallbackQuery):
    member_id = int(callback.data.split("_")[2])
    member = await get_member_details(member_id)

    text = (
        f"🏠 Точка: <b>{member['group_name']}</b>\n"
        f"👤 Котенок: <b>{member['full_name']}</b>\n"
        f"💰 Налички осталось: <b>{member['balance']} руб.</b>"
    )
    kb = get_member_menu_kb(member_id, member['group_id'], member['is_set'], member['balance'])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


# --- ФИНАНСОВЫЕ ОПЕРАЦИИ ---

@router.callback_query(F.data.startswith("set_balance_"))
async def start_set_balance(callback: CallbackQuery, state: FSMContext):
    member_id = int(callback.data.split("_")[2])
    await state.update_data(member_id=member_id)
    msg = await callback.message.edit_text("Введите сумму:", reply_markup=get_cancel_kb())
    await state.update_data(msg_id=msg.message_id)
    await state.set_state(SetBalanceState.waiting_for_amount)


@router.message(SetBalanceState.waiting_for_amount)
async def process_set_balance(message: types.Message, state: FSMContext):
    with suppress(TelegramBadRequest): await message.delete()
    if not message.text.isdigit(): return

    amount = int(message.text)
    data = await state.get_data()
    with suppress(TelegramBadRequest): await bot.delete_message(message.chat.id, data['msg_id'])

    await update_balance(data['member_id'], amount, 'set')

    member = await get_member_details(data['member_id'])
    text = (
        f"🏠 Точка: <b>{member['group_name']}</b>\n"
        f"👤 Котенок: <b>{member['full_name']}</b>\n"
        f"💰 Налички осталось: <b>{member['balance']} руб.</b>"
    )
    kb = get_member_menu_kb(member['id'], member['group_id'], member['is_set'], member['balance'])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data.startswith("withdraw_"))
async def start_withdraw(callback: CallbackQuery, state: FSMContext):
    member_id = int(callback.data.split("_")[1])
    await state.update_data(member_id=member_id)

    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="❌ Отмена", callback_data=f"view_member_{member_id}"))
    msg = await callback.message.edit_text("Сколько забрать?", reply_markup=kb.as_markup())

    await state.update_data(msg_id=msg.message_id)
    await state.set_state(WithdrawState.waiting_for_amount)


@router.message(WithdrawState.waiting_for_amount)
async def process_withdraw(message: types.Message, state: FSMContext):
    with suppress(TelegramBadRequest): await message.delete()
    if not message.text.isdigit(): return

    amount = int(message.text)
    data = await state.get_data()
    with suppress(TelegramBadRequest): await bot.delete_message(message.chat.id, data['msg_id'])

    await update_balance(data['member_id'], amount, 'withdraw')

    member = await get_member_details(data['member_id'])
    text = (
        f"🏠 Точка: <b>{member['group_name']}</b>\n"
        f"👤 Котенок: <b>{member['full_name']}</b>\n"
        f"💰 Налички осталось: <b>{member['balance']} руб.</b>"
    )
    kb = get_member_menu_kb(member['id'], member['group_id'], member['is_set'], member['balance'])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data.startswith("history_"))
async def show_history(callback: CallbackQuery):
    member_id = int(callback.data.split("_")[1])
    transactions = await get_transactions(member_id)
    member = await get_member_details(member_id)

    text = f"📜 <b>История ({member['full_name']}):</b>\n\n"
    if not transactions:
        text += "Пусто"
    else:
        for tr in transactions:
            type_icon = "➕" if tr['operation_type'] == 'Начальный взнос' else "➖"
            text += f"{type_icon} {tr['created_at']} — <b>{tr['amount']} р.</b>\n"

    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"view_member_{member_id}"))
    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")


async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())