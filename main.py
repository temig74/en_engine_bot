import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ContentType

import configparser
import sys
import io
from typing import Union
import re

from encounter_bot import EncounterBot
import logging
logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stdout)])

try:
    config = configparser.ConfigParser()
    config.read('tg_settings.ini', encoding='utf-8')
    ADMIN_USERNAMES = tuple(map(lambda s: s.strip().lower(), config['Settings']['Admins'].split(',')))  # Администраторы, которым разрешена авторизация бота в чате
    TASK_MAX_LEN = int(config['Settings']['Task_max_len'])  # Максимальное кол-во символов в одном сообщении, если превышает, то разбивается на несколько
    MAX_SCREEN_HEIGHT = int(config['Settings']['Max_screen_height'])
    TG_TOKEN = config['Settings']['Token']
except Exception as se:
    logging.error(f"Error reading settings.ini config: {se}")
    exit(1)

TG_BOT = Bot(token=TG_TOKEN)
dp = Dispatcher()
EN_BOT: EncounterBot | None = None


class CmdFilter(BaseFilter):
    def __init__(self, commands: list[str], args_count: Union[list[int], None]):
        self.commands = commands
        self.args_count = args_count

    async def __call__(self, message: Message, state: FSMContext):
        text = message.text
        if not text:
            return False
        # Проверка, что первый символ /
        if text[0] != '/':
            return False
        input_split = text.split()
        # Проверка, что количество аргументов соответствует
        if self.args_count and len(input_split)-1 not in self.args_count:
            return False
        command = input_split[0][1:].split('@')[0].lower()
        # Проверка, что команда соответствует списку команд
        if command not in self.commands:
            return False
        args = input_split[1:] if len(input_split) > 1 else None
        peer_id = message.chat.id
        return {'command': command, 'args': args, 'peer_id': peer_id, 'from_': message.from_user.username}


def add_coords_copy(text):
    # escape tags
    escaped_text = re.sub(r'<(?!code>)([^>]*)>', lambda match: f'&lt;{match.group(1)}&gt;', text)
    # add <code> to coords
    coords_pattern = r'((?<![@1234567890-])-?\d{1,2}\.\d{3,10}[, ]*-?\d{1,3}\.\d{3,10})'
    return re.sub(coords_pattern, r'<code>\1</code>', escaped_text)


async def sender_function(peer_id, message):
    if isinstance(message, str):
        for i in range(0, len(message), TASK_MAX_LEN):
            await TG_BOT.send_message(peer_id, add_coords_copy(message[i:i + TASK_MAX_LEN]), parse_mode='HTML')
    if isinstance(message, io.BytesIO):
        buf_file = types.BufferedInputFile(message.getvalue(), filename=message.name)
        if message.name.endswith('.png') and (not message.name.split('_')[0].isdigit() or int(message.name.split('_')[0]) < MAX_SCREEN_HEIGHT):
            await TG_BOT.send_photo(peer_id, buf_file)
        else:
            await TG_BOT.send_document(peer_id, buf_file)
    if isinstance(message, list):
        await TG_BOT.send_location(peer_id, message[0], message[1])


# далее команды бота
@dp.message(CmdFilter(['help', 'start'], [0]))
async def cmd_help(message: Message):
    await message.answer(r'''Temig tg enbot
    https://github.com/temig74
    /help, /start - этот help
    /auth домен id_игры логин пароль [id_чата] - авторизовать бота на игру в игровом чате (или в личке, добавив id_чата)
    /stop_auth - отключить чат
    /get_id - получить id чата и пользователя
    /game_monitor [0] - включить/[отключить] слежение за игрой
    /s, /sectors [level№] - показать сектора [прошедшего_уровня]
    /sectors_left - оставшиеся сектора на уровне
    /b, /bonuses [level№] - показать бонусы [прошедшего_уровня]
    /h, /hints - показать подсказки
    /t, /task - показать текущее задание
    /screen, /скрин - скриншот текущего уровня
    /fscreen, /фскрин - полный скриншот текущего уровня
    /любой_код123 - вбитие в движок любой_код123
    /!любой_код123 - вбитие в сектор любой_код123 (актуально при блокировке)
    /accept_codes [0] - включить/[выключить] прием кодов из чата
    /sector_monitor [0] - включить/[выключить] мониторинг секторов
    /bonus_monitor [0] - включить/[выключить] мониторинг бонусов
    /parser [0] - включить/[выключить] парсер HTML
    /send_screen [0] - включить/[выключить] отправку скрина нового уровня
    /send_code_in_block [0] - включить/[выключить] отправку кодов в сектор при блокировке (без !)
    /route_builder [0] - включить/[выключить] автомобильный построитель маршрутов. Необходим Api-ключ Яндекса "JavaScript API и HTTP Геокодер"
    /up_full_screen [0] - включить/[выключить] полноэкранный скриншот при апе уровня (если снятие скриншотов включено)
    /set_coords xx.xxxxxx yy.yyyyyy - установить текущие координаты (для построителя маршрутов)
    /time - оставшееся время до апа
    /load_old_json - загрузить информацию о прошедших уровнях игры из файла (при перезапуске бота)
    /geo или /* координаты через пробел - отправить геометку по координатам
    /set_players @игрок1 @игрок2... - установить список полевых игроков
    /open_browser открыть бразуер на компьютере, где запущен бот, привязанный к сессии бота
    /game_info - информация об игре
    /set_doc - установить ссылку на гуглдок
    /w название_статьи - скрин статьи из вики
    /wf название_статьи - полный скрин статьи из вики
    /leave_chat id_чата - покинуть чат с указанным id
    ''')


@dp.message(CmdFilter(['auth'], [0, 4, 5]))
async def cmd_auth(message: Message, args: list[str], peer_id: int, from_):
    if from_ not in ADMIN_USERNAMES:
        await message.answer('Недостаточно прав для авторизации бота')
        return
    if not args:
        await message.answer('Введите команду в формате /auth домен id_игры логин пароль [id_чата]')
    if len(args) == 5 and args[4].lstrip('-').isdigit():
        cur_chat_id = int(args[4])
    elif len(args) == 4:
        cur_chat_id = peer_id
    else:
        await message.answer('Неверный формат id чата')
        return
    if not args[1].isdigit():
        await message.answer('Неверный формат id игры')
        return
    my_domain, my_game_id, my_login, my_password = args[:4]
    await EN_BOT.auth(cur_chat_id, my_domain, my_game_id, my_login, my_password)


@dp.message(CmdFilter(['screen', 'скрин', 'fscreen', 'фскрин'], [0]))
async def cmd_screen(message: Message, command: str, peer_id: int):
    full = command in ['fscreen', 'фскрин']
    screen_bytes = await EN_BOT.get_screen_as_bytes_async(peer_id, full)
    await sender_function(peer_id, screen_bytes)


@dp.message(CmdFilter(['game_monitor'], [0, 1]))
async def cmd_game_monitor(message: Message, args: list[str], peer_id: int):
    if args and args[0] == '0':
        state = False
    else:
        state = True
    await EN_BOT.game_monitor(peer_id, state)


@dp.message(CmdFilter(['stop_auth'], [0]))
async def cmd_stop_auth(message: Message, peer_id: int, from_: str):
    if from_ not in ADMIN_USERNAMES:
        await message.answer('Недостаточно прав для отключения авторизации бота')
        return
    await EN_BOT.stop_auth(peer_id)


@dp.message(CmdFilter(['get_id'], [0]))
async def cmd_get_id(message: Message, peer_id: int, from_: str):
    await message.answer(f'id чата: <code>{peer_id}</code>\nпользователь: {from_}', parse_mode='HTML')


@dp.message(CmdFilter(['h', 'hints'], [0]))
async def cmd_hint(message: Message, peer_id: int):
    hint_str = await EN_BOT.get_hints(peer_id)
    await sender_function(peer_id, hint_str)


@dp.message(CmdFilter(['t', 'task'], [0]))
async def cmd_task(message: Message, peer_id: int):
    task_str = await EN_BOT.get_task(peer_id)
    await sender_function(peer_id, task_str)
    hint_str = await EN_BOT.get_hints(peer_id)
    await sender_function(peer_id, hint_str)


@dp.message(CmdFilter(['open_browser'], [0]))
async def cmd_open_browser(message: Message, peer_id: int, from_: str):
    if from_ not in ADMIN_USERNAMES:
        await message.answer('Недостаточно прав для запуска браузера')
        return
    await EN_BOT.open_browser(peer_id)


@dp.message(CmdFilter(['time'], [0]))
async def cmd_time(message: Message, peer_id: int):
    time_str = await EN_BOT.get_time(peer_id)
    await sender_function(peer_id, time_str)


@dp.message(CmdFilter(['s', 'sectors', 'sectors_left', 'b', 'bonuses'], [0, 1]))
async def cmd_sectors(message: Message, command: str, args: list[str], peer_id: int):
    sector = True if command in ['s', 'sectors', 'sectors_left'] else False
    levelnum = args[0] if args else '0'
    result_str = await EN_BOT.get_sectors_and_bonuses(peer_id, sector, levelnum, True if command == 'sectors_left' else False)
    await sender_function(peer_id, result_str)


@dp.message(CmdFilter(['load_old_json'], [0]))
async def cmd_load_old_json(message: Message, peer_id: int):
    await EN_BOT.load_old_json(peer_id)


@dp.message(CmdFilter(['accept_codes', 'sector_monitor', 'bonus_monitor', 'send_screen', 'parser', 'send_code_in_block', 'route_builder', 'up_full_screen'], [0, 1]))
async def switch_flag(message: Message, command: str, args: list[str], peer_id: int):
    switch = False if (args and args[0] == '0') else True
    await EN_BOT.switch_flag(peer_id, command, switch)


@dp.message(CmdFilter(['set_players'], None))
async def cmd_set_players(message: Message, args: list[str], peer_id: int):
    await EN_BOT.set_players(peer_id, args)


@dp.message(CmdFilter(['set_doc'], [0, 1]))
async def cmd_set_doc(message: Message, args: list[str], peer_id: int):
    await EN_BOT.set_doc(peer_id, args[0] if args else None)


@dp.message(CmdFilter(['set_coords'], [2]))
async def cmd_set_coords(message: Message, peer_id: int):
    coords = message.text.replace(',', ' ').split()[1:3]
    await EN_BOT.set_coords(peer_id, coords)


@dp.message(CmdFilter(['game_info'], [0]))
async def cmd_game_info(message: Message, peer_id: int):
    game_str = await EN_BOT.get_game_info(peer_id)
    await sender_function(peer_id, game_str)


@dp.message(CmdFilter(['*', 'geo'], [0, 2]))
async def cmd_geo(message: Message, args: list[str], peer_id: int):
    if args:
        await sender_function(peer_id, [args])
    else:
        await message.answer('Введите широту и долготу после команды через пробел')


@dp.message(CmdFilter(['w', 'wf'], None))
async def cmd_w(message: Message, command: str, args: list[str], peer_id: int):
    if args:
        article = ' '.join(args)
    else:
        await message.answer('Введите название статьи после команды')
        return
    full = (command == 'wf')
    screen_bytes = await EN_BOT.get_screen_as_bytes_async(peer_id, full, article)
    await sender_function(peer_id, screen_bytes)


@dp.message(CmdFilter(['leave_chat'], [1]))
async def cmd_leave_chat(message: Message, args: list[str], from_: str):
    if from_ not in ADMIN_USERNAMES:
        await message.answer('Недостаточно прав для выхода из чата')
        return
    try:
        await TG_BOT.leave_chat(args[0])
    except:
        await message.answer('Ошибка выхода из чата')


@dp.message(F.content_type == ContentType.TEXT)
async def send_answer(message: Message):
    answer = message.text[1:]
    if answer[0] == '!':
        answer = answer[1:]
        send_to_sector = True
    else:
        send_to_sector = False
    answer_reply = await EN_BOT.send_answer(message.chat.id, f'{message.from_user.username}', answer, send_to_sector)
    await sender_function(message.chat.id, answer_reply)


async def main():
    global EN_BOT
    EN_BOT = await EncounterBot.create(sender_function)
    await TG_BOT.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(TG_BOT)

if __name__ == '__main__':
    asyncio.run(main())
