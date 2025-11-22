import asyncio
import logging
import aiosqlite
from datetime import datetime
from contextlib import suppress
from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = "7439318234:AAEa9uF3-OAbVBj6xX7ODOd6vjSIZb48WFQ"
DB_NAME = "cash_calc.db"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)


# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS points (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                user_id INTEGER, 
                name TEXT, 
                target INTEGER
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                point_id INTEGER, 
                amount INTEGER, 
                created_at TEXT
            )
        ''')
        await db.commit()


async def upsert_user(user_id, full_name):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            INSERT INTO users (user_id, full_name) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name
        ''', (user_id, full_name))
        await db.commit()


async def add_point_to_db(user_id, name, target):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('INSERT INTO points (user_id, name, target) VALUES (?, ?, ?)', (user_id, name, target))
        await db.commit()


async def get_unique_point_names():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('''
            SELECT name 
            FROM points 
            GROUP BY LOWER(name)
            ORDER BY name ASC
        ''') as cursor:
            return await cursor.fetchall()


async def get_employees_by_point_name(point_name):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        sql = '''
            SELECT p.id, p.target, u.full_name 
            FROM points p
            JOIN users u ON p.user_id = u.user_id
            WHERE LOWER(p.name) = LOWER(?)
        '''
        async with db.execute(sql, (point_name,)) as cursor:
            return await cursor.fetchall()


async def get_point_details(point_id):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        sql = '''
            SELECT p.*, u.full_name 
            FROM points p
            JOIN users u ON p.user_id = u.user_id
            WHERE p.id = ?
        '''
        async with db.execute(sql, (point_id,)) as cursor:
            return await cursor.fetchone()


async def decrease_point_balance(point_id, amount):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('UPDATE points SET target = target - ? WHERE id = ?', (amount, point_id))
        await db.commit()


async def add_transaction(point_id, amount):
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('INSERT INTO transactions (point_id, amount, created_at) VALUES (?, ?, ?)',
                         (point_id, amount, now))
        await db.commit()


async def get_transactions(point_id):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                'SELECT amount, created_at FROM transactions WHERE point_id = ? ORDER BY id DESC LIMIT 10',
                (point_id,)) as cursor:
            return await cursor.fetchall()


async def delete_point_from_db(point_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('DELETE FROM points WHERE id = ?', (point_id,))
        await db.execute('DELETE FROM transactions WHERE point_id = ?', (point_id,))
        await db.commit()


# --- АНИМАЦИЯ УДАЛЕНИЯ ---
async def play_delete_animation(message: types.Message):
    base_row = "❌❌❌❌❌"
    for rows in range(5, 0, -1):
        text = "\n".join([base_row] * rows)
        with suppress(TelegramBadRequest):
            await message.edit_text(text)
        await asyncio.sleep(0.25)

    for chars in range(4, -1, -1):
        text = "❌" * chars
        if not text: text = "🗑 Удалено."
        with suppress(TelegramBadRequest):
            await message.edit_text(text)
        await asyncio.sleep(0.2)


# --- FSM ---
class AddPoint(StatesGroup):
    waiting_for_name = State()
    waiting_for_amount = State()


class WithdrawCash(StatesGroup):
    waiting_for_amount = State()


# --- КЛАВИАТУРЫ ---

def get_start_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="💰 Калькулятор налички", callback_data="open_calc"))
    return builder.as_markup()


async def get_folders_keyboard():
    builder = InlineKeyboardBuilder()
    unique_names = await get_unique_point_names()

    for row in unique_names:
        builder.row(InlineKeyboardButton(text=f"📂 {row['name']}", callback_data=f"folder_{row['name']}"))

    builder.row(InlineKeyboardButton(text="➕ Добавить свою точку", callback_data="add_point"))
    # [ДОБАВЛЕНО] Кнопка Назад в главное меню
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main_menu"))
    return builder.as_markup()


async def get_employees_keyboard(point_name):
    builder = InlineKeyboardBuilder()
    points = await get_employees_by_point_name(point_name)

    for row in points:
        user_name = row['full_name'] if row['full_name'] else "Сотрудник"
        builder.row(
            InlineKeyboardButton(text=f"👤 {user_name} ({row['target']} р.)", callback_data=f"view_point_{row['id']}"))

    builder.row(InlineKeyboardButton(text="⬅️ Назад к папкам", callback_data="open_calc"))
    return builder.as_markup()


def get_point_menu_keyboard(point_id, point_name):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💸 Забрать наличку", callback_data=f"withdraw_{point_id}"))
    builder.row(InlineKeyboardButton(text="📜 История операций", callback_data=f"history_{point_id}"))
    builder.row(InlineKeyboardButton(text="❌ Удалить точку", callback_data=f"ask_delete_{point_id}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад к сотрудникам", callback_data=f"folder_{point_name}"))
    return builder.as_markup()


def get_back_to_point_keyboard(point_id):
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"view_point_{point_id}"))
    return builder.as_markup()


def get_confirm_delete_keyboard(point_id):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"confirm_delete_{point_id}"))
    builder.row(InlineKeyboardButton(text="🔙 Нет, отмена", callback_data=f"view_point_{point_id}"))
    return builder.as_markup()


def get_back_to_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="⬅️ Назад", callback_data="open_calc"))
    return builder.as_markup()


# --- ЛОГИКА ---

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await upsert_user(message.from_user.id, message.from_user.full_name)
    await message.answer("<b>Главное меню</b>", reply_markup=get_start_keyboard(), parse_mode="HTML")
    with suppress(TelegramBadRequest):
        await message.delete()


# [ДОБАВЛЕНО] Обработка кнопки Назад в главное меню
@router.callback_query(F.data == "to_main_menu")
async def back_to_main_menu(callback: CallbackQuery):
    await callback.message.edit_text("<b>Главное меню</b>", reply_markup=get_start_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "open_calc")
async def open_calculator_folders(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await upsert_user(callback.from_user.id, callback.from_user.full_name)

    kb = await get_folders_keyboard()
    try:
        await callback.message.edit_text("<b>📂 Выберите точку:</b>", reply_markup=kb, parse_mode="HTML")
    except:
        await callback.message.answer("<b>📂 Выберите точку:</b>", reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("folder_"))
async def open_folder(callback: CallbackQuery):
    point_name = callback.data.split("_")[1]
    kb = await get_employees_keyboard(point_name)
    await callback.message.edit_text(
        f"🏪 Точка: <b>{point_name}</b>\n👤 Выберите сотрудника:",
        reply_markup=kb,
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("view_point_"))
async def view_point(callback: CallbackQuery):
    point_id = int(callback.data.split("_")[2])
    point = await get_point_details(point_id)

    if not point:
        await callback.answer("Точка не найдена", show_alert=True)
        return

    text = (
        f"🏪 Точка: <b>{point['name']}</b>\n"
        f"👤 Пользователь: <b>{point['full_name']}</b>\n"
        f"💰 В кассе: <b>{point['target']} руб.</b>"
    )
    await callback.message.edit_text(text, reply_markup=get_point_menu_keyboard(point_id, point['name']),
                                     parse_mode="HTML")


# --- ДОБАВЛЕНИЕ ТОЧКИ ---
@router.callback_query(F.data == "add_point")
async def start_add_point(callback: CallbackQuery, state: FSMContext):
    msg = await callback.message.edit_text("Введите <b>название</b> точки (например: Амбар):",
                                           reply_markup=get_back_to_menu_keyboard(), parse_mode="HTML")

    await state.update_data(msg_to_delete=msg.message_id)
    await state.set_state(AddPoint.waiting_for_name)


@router.message(AddPoint.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    with suppress(TelegramBadRequest): await message.delete()

    data = await state.get_data()
    if 'msg_to_delete' in data:
        with suppress(TelegramBadRequest):
            await bot.delete_message(chat_id=message.chat.id, message_id=data['msg_to_delete'])

    await state.update_data(point_name=message.text)

    msg = await message.answer(f"Точка: <b>{message.text}</b>\nСколько нужно набрать?",
                               reply_markup=get_back_to_menu_keyboard(), parse_mode="HTML")

    await state.update_data(msg_to_delete=msg.message_id)
    await state.set_state(AddPoint.waiting_for_amount)


@router.message(AddPoint.waiting_for_amount)
async def process_amount(message: types.Message, state: FSMContext):
    with suppress(TelegramBadRequest):
        await message.delete()

    data = await state.get_data()
    if 'msg_to_delete' in data:
        with suppress(TelegramBadRequest):
            await bot.delete_message(chat_id=message.chat.id, message_id=data['msg_to_delete'])

    if not message.text.isdigit():
        temp = await message.answer("❌ Введите число!", reply_markup=get_back_to_menu_keyboard())
        await asyncio.sleep(2)
        with suppress(TelegramBadRequest): await temp.delete()
        return

    await upsert_user(message.from_user.id, message.from_user.full_name)
    await add_point_to_db(message.from_user.id, data['point_name'], int(message.text))

    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="⬅️ К списку точек", callback_data="open_calc"))
    await message.answer(f"✅ Точка <b>{data['point_name']}</b> создана!", reply_markup=kb.as_markup(),
                         parse_mode="HTML")


# --- ОПЕРАЦИИ ---

@router.callback_query(F.data.startswith("history_"))
async def view_history(callback: CallbackQuery):
    point_id = int(callback.data.split("_")[1])
    transactions = await get_transactions(point_id)
    point = await get_point_details(point_id)

    text = f"📜 <b>История ({point['name']} / {point['full_name']}):</b>\n\n"
    if not transactions:
        text += "<i>Пусто</i>"
    else:
        for tr in transactions:
            text += f"➖ {tr['created_at']} — <b>{tr['amount']} руб.</b>\n"

    await callback.message.edit_text(text, reply_markup=get_back_to_point_keyboard(point_id), parse_mode="HTML")


@router.callback_query(F.data.startswith("withdraw_"))
async def start_withdraw(callback: CallbackQuery, state: FSMContext):
    point_id = int(callback.data.split("_")[1])
    await state.update_data(current_point_id=point_id)

    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="❌ Отмена", callback_data=f"view_point_{point_id}"))

    msg = await callback.message.edit_text("Сколько забрали?", reply_markup=kb.as_markup(), parse_mode="HTML")
    await state.update_data(withdraw_msg_id=msg.message_id)
    await state.set_state(WithdrawCash.waiting_for_amount)
    await callback.answer()


@router.message(WithdrawCash.waiting_for_amount)
async def process_withdraw(message: types.Message, state: FSMContext):
    with suppress(TelegramBadRequest):
        await message.delete()

    data = await state.get_data()
    if 'withdraw_msg_id' in data:
        with suppress(TelegramBadRequest): await bot.delete_message(chat_id=message.chat.id,
                                                                    message_id=data['withdraw_msg_id'])

    if not message.text.isdigit():
        temp = await message.answer("Число!")
        await asyncio.sleep(1)
        with suppress(TelegramBadRequest): await temp.delete()
        return

    amount = int(message.text)
    point_id = data['current_point_id']

    await add_transaction(point_id, amount)
    await decrease_point_balance(point_id, amount)
    point = await get_point_details(point_id)

    await state.clear()
    await message.answer(
        f"✅ Забрали: <b>{amount} руб.</b>\n👤 Сотрудник: <b>{point['full_name']}</b>\n💰 Осталось: <b>{point['target']} руб.</b>",
        reply_markup=get_back_to_point_keyboard(point_id),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("ask_delete_"))
async def ask_delete_point(callback: CallbackQuery):
    point_id = int(callback.data.split("_")[2])
    point = await get_point_details(point_id)
    await callback.message.edit_text(
        f"⚠️ Удалить точку <b>{point['name']}</b> (сотрудник: {point['full_name']})?",
        reply_markup=get_confirm_delete_keyboard(point_id),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("confirm_delete_"))
async def confirm_delete_point(callback: CallbackQuery):
    point_id = int(callback.data.split("_")[2])

    await play_delete_animation(callback.message)
    await delete_point_from_db(point_id)

    kb_back = InlineKeyboardBuilder()
    kb_back.add(InlineKeyboardButton(text="⬅️ Назад", callback_data="open_calc"))

    await asyncio.sleep(0.5)
    await callback.message.edit_text("🗑 <b>Удалено.</b>", reply_markup=kb_back.as_markup(), parse_mode="HTML")


async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())