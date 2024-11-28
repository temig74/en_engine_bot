import datetime
import json
from time import sleep
import requests
import telebot  # pip install pyTelegramBotAPI
import threading
import base64
from selenium import webdriver  # pip install selenium
from selenium.webdriver.firefox.options import Options  # Need installed Firefox in system

from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
# from selenium.webdriver.common.by import By

import io
import re
import os.path
import sys
import configparser

# from bs4 import BeautifulSoup
# import curlify
# import webbrowser

# Читаем конфиг
config = configparser.ConfigParser()
config.read('settings.ini')
ADMIN_USERNAMES = tuple(config['Settings']['Admins'].split(','))  # Администраторы, которым разрешена авторизация бота в чате
SECTORS_LEFT_ALERT = int(config['Settings']['Sectors_left_alert'])  # Количество оставшихся для закрытия секторов, с которого выводить оповещение, сколько осталось
USER_AGENT = {'User-agent': config['Settings']['User_agent']}  # Выставляемый в requests и selenium user-agent
TASK_MAX_LEN = int(config['Settings']['Task_max_len'])  # Максимальное кол-во символов в одном сообщении, если превышает, то разбивается на несколько
YANDEX_API_KEY = config['Settings']['Yandex_api_key']
MAP_TYPE = config['Settings']['Map_type']
MAP_BROWSER_SLEEP = int(config['Settings']['Map_browser_sleep'])
MAP_BROWSER_TIMEOUT = int(config['Settings']['Map_browser_timeout'])
LANG = config['Settings']['Lang']
CHECK_INTERVAL = int(config['Settings']['Check_interval'])
TIMELEFT_ALERT1 = int(config['Settings']['Timeleft_alert1'])
TIMELEFT_ALERT2 = int(config['Settings']['Timeleft_alert2'])

with open('yandex_api.txt', 'r', encoding='utf8') as yandex_api_file:
    YANDEX_API_PATTERN = yandex_api_file.read()

executable_dir = os.path.dirname(sys.executable)
folder_path = os.path.join(executable_dir, 'level_snapshots')
print(folder_path)
if not os.path.exists(folder_path):
    os.makedirs(folder_path)

CUR_PARAMS = {}                 # словарь с текущими состояниями слежения в чатах
telebot.apihelper.ENABLE_MIDDLEWARE = True  # Разрешаем MIDDLEWARE до создания бота
BOT = telebot.TeleBot(config['Settings']['Token'], num_threads=int(config['Settings']['Threads']))  # еще вариант с потоками threaded=False


# Предварительная обработка команд
@BOT.middleware_handler(update_types=['message'])
def modify_message(bot_instance, message):
    if message.text is None:
        return
    cmd = message.text.split('@')[0].split()[0].lower()[1:]
    # Запрет всех команд в чате, кроме тех, которые могут работать в неавторизованном чате, перенаправляем на handler INCORRECT_CHAT
    if cmd not in ('help', 'start', 'auth', 'get_chat_id', '*', 'geo', 'leave_chat', 'test') and message.chat.id not in CUR_PARAMS:
        message.text = '/incorrect_chat'
        return
    # Запрет авторизации и загрузки из файла от всех, кроме админов, перенаправляем INCORRECT_USER
    if cmd in ('auth', 'stop_auth', 'load_old_json', 'open_browser', 'leave_chat') and message.from_user.username not in ADMIN_USERNAMES:
        message.text = '/incorrect_user'
        return


# Парсинг текста на список координат и файл KML
def gen_kml2(text):
    # coord_list = re.findall(r'-?\d{1,2}\.\d{3,10}[, ]*-?\d{1,3}\.\d{3,10}', text)
    coord_list = re.findall(r'(?<![@1234567890-])-?\d{1,2}\.\d{3,10}[, ]*-?\d{1,3}\.\d{3,10}', text)
    if not coord_list:
        return
    result_list = []
    kml = '<kml><Document>'
    for cnt, elem in enumerate(coord_list):
        c = re.findall(r'-?\d{1,3}\.\d{3,10}', elem)
        new_point = f'<Point><coordinates>{c[1]},{c[0]},0.0</coordinates></Point>'
        if new_point not in kml:
            kml += f'<Placemark><name>Point {cnt+1}</name>{new_point}</Placemark>'
            result_list.append((c[0], c[1]))
    kml += '</Document></kml>'
    buf_file = io.StringIO()
    buf_file.write(kml)
    buf_file.seek(0, 0)
    return buf_file, result_list  # Возвращаем кортеж из файла kml и списка координат


def add_coords_copy(text):
    pattern = r'((?<![@1234567890-])-?\d{1,2}\.\d{3,10}[, ]*-?\d{1,3}\.\d{3,10})'
    return re.sub(pattern, r'`\1`', text)


# Функция отправки kml и координат
def send_kml_info(cur_chat, parse_text, level_num):
    kml_var = gen_kml2(parse_text)
    if kml_var:
        kml_str = ''
        for elem in kml_var[1]:
            kml_str += '`' + elem[0] + ' ' + elem[1] + '`\n'
        kml_var[0].name = f'points{level_num}.kml'
        BOT.send_document(cur_chat, kml_var[0], caption=kml_str, parse_mode='MarkDown')
        BOT.send_venue(cur_chat, kml_var[1][0][0], kml_var[1][0][1], f'{kml_var[1][0][0]}, {kml_var[1][0][1]}', '')
        last_coords = CUR_PARAMS[cur_chat]['last_coords']

        # Если включен построитель маршрутов и последние координаты не совпадают с прошлыми, то кидаем картинки доезда
        if last_coords and CUR_PARAMS[cur_chat]['route_builder'] and last_coords != (kml_var[1][0][0], kml_var[1][0][1]):
            fox = CUR_PARAMS[cur_chat].get('driver')
            if fox:
                html_bs64 = base64.b64encode(YANDEX_API_PATTERN.replace('#coords1', f'{last_coords[0]},{last_coords[1]}').replace('#coords2', f'{kml_var[1][0][0]}, {kml_var[1][0][1]}').replace('#my_api_key', YANDEX_API_KEY).replace('#bounds_flag', 'false').replace('#map_type', MAP_TYPE).replace('loaded', 'loaded1').encode('utf-8')).decode()
                fox.get('data:text/html;base64,'+html_bs64)
                WebDriverWait(fox, MAP_BROWSER_TIMEOUT).until(EC.title_is('loaded1'))
                sleep(MAP_BROWSER_SLEEP)
                BOT.send_photo(cur_chat, base64.b64decode(fox.get_full_page_screenshot_as_base64()), caption='Начало доезда')

                html_bs64 = base64.b64encode(YANDEX_API_PATTERN.replace('#coords1', f'{last_coords[0]},{last_coords[1]}').replace('#coords2', f'{kml_var[1][0][0]}, {kml_var[1][0][1]}').replace('#my_api_key', YANDEX_API_KEY).replace('#bounds_flag', 'true').replace('#map_type', MAP_TYPE).replace('loaded', 'loaded2').encode('utf-8')).decode()
                fox.get('data:text/html;base64,'+html_bs64)
                WebDriverWait(fox, MAP_BROWSER_TIMEOUT).until(EC.title_is('loaded2'))
                sleep(MAP_BROWSER_SLEEP)
                BOT.send_photo(cur_chat, base64.b64decode(fox.get_full_page_screenshot_as_base64()), caption='Весь доезд')
                '''fox.get(f'https://yandex.ru/maps/?l=sat&ll={last_coords[1]}%2C{last_coords[0]}&mode=routes&rtext={last_coords[0]}%2C{last_coords[1]}~{kml_var[1][0][0]}%2C{kml_var[1][0][1]}&rtt=auto&ruri=~&z=16')
                sleep(2)
                BOT.send_photo(cur_chat, base64.b64decode(fox.find_element(By.CLASS_NAME, "map-container").screenshot_as_base64), caption='Начало доезда')'''

        CUR_PARAMS[cur_chat]['last_coords'] = (kml_var[1][0][0], kml_var[1][0][1])


# Отправить информацию о текущем уровне
def send_curlevel_info(cur_chat, cur_json):
    # Выводим информацию о номере уровня, автопереходе, блокировке ответов
    gameinfo_str = f'Уровень {cur_json["Level"]["Number"]} из {len(cur_json["Levels"])} {cur_json["Level"]["Name"]}\n'
    gameinfo_str += f'Выполнить секторов: {cur_json["Level"]["RequiredSectorsCount"] if cur_json["Level"]["RequiredSectorsCount"] > 0 else 1} из {len(cur_json["Level"]["Sectors"]) if len(cur_json["Level"]["Sectors"]) > 0 else 1}\n'
    if cur_json["Level"]["Messages"]:
        gameinfo_str += 'Сообщения на уровне:\n'
        for elem in cur_json["Level"]["Messages"]:
            gameinfo_str += elem["MessageText"]+'\n'

    if cur_json["Level"]["Timeout"] > 0:
        gameinfo_str += f'Автопереход через {datetime.timedelta(seconds=cur_json["Level"]["Timeout"])}\n'
    else:
        gameinfo_str += 'Автопереход отсутствует\n'
    if cur_json["Level"]["HasAnswerBlockRule"]:
        gameinfo_str += f'ВНИМАНИЕ, БЛОКИРОВКА ОТВЕТОВ! НЕ БОЛЕЕ {cur_json["Level"]["AttemtsNumber"]} ПОПЫТОК ЗА {datetime.timedelta(seconds=cur_json["Level"]["AttemtsPeriod"])} ДЛЯ {"КОМАНДЫ" if cur_json["Level"]["BlockTargetId"] == 2 else "ИГРОКА"}'
    BOT.send_message(cur_chat, gameinfo_str)

    # Отдельно выводим задание
    if len(cur_json['Level']['Tasks']) > 0:
        # gamelevel_str = cur_json['Level']['Tasks'][0]['TaskText']
        gamelevel_str = add_coords_copy(cur_json['Level']['Tasks'][0]['TaskText'])
    else:
        gamelevel_str = 'Нет заданий на уровне'

    # Если очень большой текст на уровне, то сплит
    for i in range(0, len(gamelevel_str), TASK_MAX_LEN):
        BOT.send_message(cur_chat, gamelevel_str[i:i + TASK_MAX_LEN], parse_mode='MarkDown')


def check_engine(cur_chat_id):
    try:
        game_json = CUR_PARAMS[cur_chat_id]["session"].get(f'https://{CUR_PARAMS[cur_chat_id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[cur_chat_id]["cur_json"]["GameId"]}?json=1&lang={LANG}').json()
    except:
        BOT.send_message(cur_chat_id, 'Ошибка мониторинга, возможно необходимо заново авторизоваться')
        return

    # False - если цикл надо прервать (Серьезная ошибка), True - если продолжать
    match game_json['Event']:
        case 2:
            BOT.send_message(cur_chat_id, 'Игра с указанным id не существует')
            return
        case 4:
            BOT.send_message(cur_chat_id, 'Ошибка авторизации')
            return
        case 5:
            print("Game hasn't started yet, continue monitoring")
            return True  # игра еще не началась, продолжаем мониторить
        case 6 | 17:
            BOT.send_message(cur_chat_id, 'Игра закончилась')
            CUR_PARAMS[cur_chat_id]['monitoring_flag'] = False
            sleep(7)
            BOT.send_message(cur_chat_id, 'Авторизация чата отключена')
            CUR_PARAMS.pop(cur_chat_id, None)  # Освобождаем в памяти словарь чата
            BOT.leave_chat(cur_chat_id)
            return
        case 7 | 8:
            BOT.send_message(cur_chat_id, 'Заявка не подана')
            return
        case 9:
            BOT.send_message(cur_chat_id, 'Команда не принята в игру')
            return
        case 10:
            BOT.send_message(cur_chat_id, 'Аккаунт не в команде')
            return
        case 11:
            BOT.send_message(cur_chat_id, 'Аккаунт не активен в команде')
            return
        case 12:
            BOT.send_message(cur_chat_id, 'Игра не содержит уровней')
            return
        case 13:
            BOT.send_message(cur_chat_id, 'Превышено количество участников')
            return
        case 16 | 18 | 21:
            BOT.send_message(cur_chat_id, 'Уровень был снят')
            check_engine(cur_chat_id)
            return True
        case 19 | 22:
            BOT.send_message(cur_chat_id, 'Уровень пройден по автопереходу')
            check_engine(cur_chat_id)
            return True
        case 20:
            check_engine(cur_chat_id)
            return True  # все секторы выполнены
        case 0:
            old_json = CUR_PARAMS[cur_chat_id]['cur_json']  # предыдущий json
            CUR_PARAMS[cur_chat_id]['cur_json'] = game_json  # текущий json

            # Игра началась
            if old_json['Level'] is None:
                BOT.send_message(cur_chat_id, 'Игра началась!\n')
                send_curlevel_info(cur_chat_id, game_json)
                return True

            # Проверка, что поменялся номер уровня, т.е. произошел АП
            if old_json['Level']['Number'] != game_json['Level']['Number']:
                CUR_PARAMS[cur_chat_id]['5_min_sent'] = False
                CUR_PARAMS[cur_chat_id]['1_min_sent'] = False
                BOT.send_message(cur_chat_id, 'АП!\n' + ' '.join(CUR_PARAMS[cur_chat_id].get('players', '')))

                # отключение ввода кодов при обнаружении штрафных
                if len(game_json['Level']['Tasks']) > 0:
                    if 'штраф' in game_json['Level']['Tasks'][0]['TaskText'].lower() or ' ложн' in game_json['Level']['Tasks'][0]['TaskText'].lower():
                        CUR_PARAMS[cur_chat_id]['accept_codes'] = False
                        BOT.send_message(cur_chat_id, 'В тексте обнаружена информация о штрафах, ввод кодов отключен! Для включения выполните /accept_codes')

                send_curlevel_info(cur_chat_id, game_json)
                if len(game_json['Level']['Tasks']) > 0:
                    send_kml_info(cur_chat_id, game_json['Level']['Tasks'][0]['TaskText'], game_json['Level']['Number'])

                # Сохраняем информацию о пройденном уровне
                CUR_PARAMS[cur_chat_id]['OLD_LEVELS'][str(old_json['Level']['Number'])] = {}
                CUR_PARAMS[cur_chat_id]['OLD_LEVELS'][str(old_json['Level']['Number'])]['Event'] = old_json['Event']
                CUR_PARAMS[cur_chat_id]['OLD_LEVELS'][str(old_json['Level']['Number'])]['Level'] = old_json['Level']

                # Запись в файл
                json_file_data = CUR_PARAMS[cur_chat_id]['OLD_LEVELS']
                json_filename = f'{cur_chat_id}.{CUR_PARAMS[cur_chat_id]["cur_json"]["GameId"]}'
                if os.path.isfile('level_snapshots/'+json_filename):
                    with open('level_snapshots/'+json_filename) as json_file:
                        json_file_data.update(json.load(json_file))
                with open('level_snapshots/'+json_filename, 'w') as json_file:
                    json.dump(json_file_data, json_file)
                return True

            # проверка на изменение текста уровня
            if old_json['Level']['Tasks'] != game_json['Level']['Tasks']:
                BOT.send_message(cur_chat_id, 'Задание уровня изменилось')

            # проверка на сообщения на уровне:
            for elem in game_json['Level']['Messages']:
                if elem not in old_json['Level']['Messages']:
                    BOT.send_message(cur_chat_id, f'Добавлено сообщение: {elem["MessageText"]}')

            # проверка на количество секторов на уровне:
            if len(old_json['Level']['Sectors']) != len(game_json['Level']['Sectors']):
                BOT.send_message(cur_chat_id, 'Количество секторов на уровне изменилось')

            # проверка на количество бонусов на уровне:
            if len(old_json['Level']['Bonuses']) != len(game_json['Level']['Bonuses']):
                BOT.send_message(cur_chat_id, 'Количество бонусов на уровне изменилось')

            # проверка на количество необходимых секторов:
            if old_json['Level']['RequiredSectorsCount'] != game_json['Level']['RequiredSectorsCount']:
                BOT.send_message(cur_chat_id, 'Количество необходимых для прохождения секторов изменилось')

            # проверка на кол-во оставшихся секторов:
            cur_sectors_left = game_json['Level']['SectorsLeftToClose']
            if old_json['Level']['SectorsLeftToClose'] != cur_sectors_left and cur_sectors_left <= SECTORS_LEFT_ALERT:
                sector_list = [str(elem['Name']) for elem in game_json['Level']['Sectors'] if not (elem['IsAnswered'])]
                BOT.send_message(cur_chat_id, f'Осталось секторов: [{cur_sectors_left}]. Оставшиеся: {", ".join(sector_list)}')

            # Проверка, что пришла подсказка
            if len(CUR_PARAMS[cur_chat_id]["cur_json"]['Level']['Helps']) != len(old_json['Level']['Helps']):
                BOT.send_message(cur_chat_id, 'Была добавлена подсказка')
            else:
                for i, elem in enumerate(CUR_PARAMS[cur_chat_id]["cur_json"]['Level']['Helps']):
                    if elem['HelpText'] != old_json['Level']['Helps'][i]['HelpText']:
                        # BOT.send_message(cur_chat_id, f'Подсказка {i + 1}: {elem["HelpText"]}')
                        BOT.send_message(cur_chat_id, f'Подсказка {i + 1}: {add_coords_copy(elem["HelpText"])}', parse_mode='MarkDown')
                        send_kml_info(cur_chat_id, elem["HelpText"], f'{CUR_PARAMS[cur_chat_id]["cur_json"]["Level"]["Number"]}_{i+1}')

            # мониторинг закрытия секторов
            if CUR_PARAMS[cur_chat_id]['sector_monitor']:
                sector_msg = ''
                for elem in game_json['Level']['Sectors']:
                    if elem not in old_json['Level']['Sectors'] and elem["IsAnswered"] and (elem['SectorId'] not in CUR_PARAMS[cur_chat_id]['sector_closers']):
                        sector_msg += f'✅№{elem["Order"]} {elem["Name"]} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]})\n'
                if sector_msg != '':
                    BOT.send_message(cur_chat_id, sector_msg)

            # мониторинг закрытия бонусов
            if CUR_PARAMS[cur_chat_id]['bonus_monitor']:
                for elem in game_json['Level']['Bonuses']:
                    if elem not in old_json['Level']['Bonuses'] and elem["IsAnswered"] and (elem['BonusId'] not in CUR_PARAMS[cur_chat_id]['sector_closers']):
                        BOT.send_message(cur_chat_id, f'{"🔴" if elem["Negative"] else "🟢"} №{elem["Number"]} {elem["Name"] or ""} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]}) {"Штраф: " if elem["Negative"] else "Бонус: "} {datetime.timedelta(seconds=elem["AwardTime"])}\n{"Подсказка бонуса:" + chr(10) + add_coords_copy(elem["Help"]) if elem["Help"] else ""}', parse_mode='MarkDown')
                        if elem["Help"]:
                            send_kml_info(cur_chat_id, elem["Help"], CUR_PARAMS[cur_chat_id]["cur_json"]["Level"]["Number"])

            # мониторинг времени до автоперехода
            if TIMELEFT_ALERT1 > game_json['Level']['TimeoutSecondsRemain'] > 0 and not (CUR_PARAMS[cur_chat_id]['5_min_sent']):
                BOT.send_message(cur_chat_id, 'До автоперехода осталось менее 5 минут!')
                CUR_PARAMS[cur_chat_id]['5_min_sent'] = True
            if TIMELEFT_ALERT2 > game_json['Level']['TimeoutSecondsRemain'] > 0 and not (CUR_PARAMS[cur_chat_id]['1_min_sent']):
                BOT.send_message(cur_chat_id, 'До автоперехода осталось менее 1 минуты!')
                CUR_PARAMS[cur_chat_id]['1_min_sent'] = True
    return True


def monitoring_func(cur_chat_id):
    start_time = datetime.datetime.now()
    BOT.send_message(cur_chat_id, 'Мониторинг включен')
    while CUR_PARAMS[cur_chat_id]['monitoring_flag']:
        print(f'Слежение за игрой в чате {cur_chat_id} работает {datetime.datetime.now()-start_time}')
        sleep(CHECK_INTERVAL)
        try:
            if not (check_engine(cur_chat_id)):
                break
        except:
            print('Ошибка функции check_engine, продолжаю мониторинг')
    CUR_PARAMS[cur_chat_id]['monitoring_flag'] = False
    BOT.send_message(cur_chat_id, 'Мониторинг выключен')


@BOT.message_handler(commands=['help', 'start'])
def send_welcome(message):
    BOT.send_message(message.chat.id, r'''Temig enbot v1.1
https://github.com/temig74/en_engine_bot/
/help - этот help
/auth домен id_игры логин пароль [id_чата] - авторизовать бота на игру в игровом чате
/stop_auth - отключить чат
/get_chat_id - получить id чата
/game_monitor [0] - включить/[отключить] слежение за игрой
/sectors [level№] - показать сектора [прошедшего_уровня]
/sectors_left - оставшиеся сектора на уровне
/bonuses [level№] - показать бонусы [прошедшего_уровня]
/hints - показать подсказки
/task - показать текущее задание
/screen - скриншот текущего уровня (необходим firefox)
/любой_код123 - вбитие в движок любой_код123
/accept_codes [0] - включить/[выключить] прием кодов из чата
/sector_monitor [0] - включить/[выключить] мониторинг секторов
/bonus_monitor [0] - включить/[выключить] мониторинг бонусов
/route_builder [0] - включить/[выключить] построитель маршрутов
/time - оставшееся время до апа
/load_old_json - загрузить информацию о прошедших уровнях игры из файла (при перезапуске бота)
/geo или /* координаты через пробел - отправить геометку по координатам
/set_players @игрок1 @игрок2 - установить список полевых игроков
/open_browser открыть бразуер на компьютере, где запущен бот, привязанный к сессии бота (необходим firefox)
/leave_chat id_чата - покинуть чат с указанным id
/game_info - информация об игре
/set_doc - установить ссылку на гуглдок
/set_coords - установить текущие координаты (для построителя маршрутов)
''', link_preview_options=telebot.types.LinkPreviewOptions(is_disabled=True))


@BOT.message_handler(commands=['auth'])
def auth(message):
    # Проверка на пользователя, у кого есть права на авторизацию бота осуществляется в middleware handler
    input_list = message.text.split()

    if len(input_list) > 6 or len(input_list) < 5:
        BOT.send_message(message.chat.id, 'Недостаточно аргументов, введите команду в формате /auth домен id_игры логин пароль [id_чата]')
        return

    if len(input_list) == 6 and input_list[5].replace('-', '').isdigit():
        cur_chat_id = int(input_list[5])
    elif len(input_list) == 5:
        cur_chat_id = message.chat.id
    else:
        BOT.send_message(message.chat.id, 'Неверный формат id чата')
        return

    if not input_list[2].isdigit():
        BOT.send_message(message.chat.id, 'Неверный формат id игры')
        return

    my_domain = input_list[1]
    my_game_id = input_list[2]
    my_login = input_list[3]
    my_password = input_list[4]
    my_session = requests.session()
    my_session.headers.update(USER_AGENT)

    try:
        auth_request_json = my_session.post(f'https://{my_domain}/login/signin?json=1', data={'Login': my_login, 'Password': my_password}).json()
    except:
        BOT.send_message(message.chat.id, 'Ошибка запроса авторизации, возможно неверно указан домен')
        return

    match auth_request_json['Error']:
        case 1:
            BOT.send_message(message.chat.id, 'Превышено количество неправильных  попыток авторизации')
            return
        case 2:
            BOT.send_message(message.chat.id, 'Неверный логин или пароль')
            return
        case 3:
            BOT.send_message(message.chat.id, 'Пользователь или в Cибири, или в черном списке, или на домене нельзя авторизовываться с других доменов')
            return
        case 4:
            BOT.send_message(message.chat.id, 'Блокировка по IP')
            return
        case 5:
            BOT.send_message(message.chat.id, 'В процессе авторизации произошла ошибка на сервере')
            return
        case 6:
            BOT.send_message(message.chat.id, 'Ошибка')
            return
        case 7:
            BOT.send_message(message.chat.id, 'Пользователь заблокирован администратором')
            return
        case 8:
            BOT.send_message(message.chat.id, 'Новый пользователь не активирован')
            return
        case 9:
            BOT.send_message(message.chat.id, 'Действия пользователя расценены как брутфорс')
            return
        case 10:
            BOT.send_message(message.chat.id, 'Пользователь не подтвердил e-mail')
            return
        case 0:
            print('Авторизация успешна')
            try:
                # Получаем информацию об игре
                cur_json = my_session.get(f'https://{my_domain}/GameEngines/Encounter/Play/{my_game_id}?json=1').json()
            except:
                BOT.send_message(message.chat.id, 'Ошибка запроса авторизации, возможно неверно указан id игры')
                return

            BOT.send_message(message.chat.id, 'Авторизация успешна')  # Только если успешна, то заново инициализируем словарь параметров чата
            CUR_PARAMS[cur_chat_id] = {
                'cur_json': cur_json,
                'session': my_session,
                'cur_domain': my_domain,
                'monitoring_flag': False,
                'accept_codes': True,
                'sector_monitor': True,
                'bonus_monitor': True,
                'route_builder': False,
                '5_min_sent': False,
                '1_min_sent': False,
                'OLD_LEVELS': {},
                'driver': None,
                'sector_closers': {},
                'bonus_closers': {},
                'last_coords': None}

            # запускаем firefox браузер, который будем использовать для скриншотов уровня и скринов маршрутов
            # print('Запускаю виртуальный браузер')
            options = Options()
            options.add_argument("--headless")  # не отображаемый в системе
            options.set_preference("general.useragent.override", USER_AGENT['User-agent'])
            my_driver = webdriver.Firefox(options=options)
            my_driver.get(f'https://{my_domain}')
            my_driver.add_cookie({'name': 'atoken', 'value': my_session.cookies.get_dict()['atoken'], 'domain': '.en.cx', 'secure': False, 'httpOnly': True, 'session': True})
            my_driver.add_cookie({'name': 'stoken', 'value': my_session.cookies.get_dict()['stoken'], 'domain': '.' + my_domain, 'secure': False, 'httpOnly': False, 'session': True})
            CUR_PARAMS[cur_chat_id]['driver'] = my_driver
            # print('Виртуальный браузер запущен')
            # CUR_PARAMS[cur_chat_id]['driver'].add_cookie({'name': 'GUID', 'value': CUR_PARAMS[cur_chat_id]['session'].cookies.get_dict()['GUID'], 'domain': CUR_PARAMS[cur_chat_id]['cur_domain'], 'secure': False, 'httpOnly': True, 'session': False})
            # r = CUR_PARAMS[cur_chat_id]['session'].get(f'https://{CUR_PARAMS[cur_chat_id]["cur_domain"]}/GameEngines/Encounter/Play/{my_game_id}')
            # print(curlify.to_curl(r.request))


@BOT.message_handler(commands=['stop_auth'])
def stop_auth(message):
    CUR_PARAMS[message.chat.id]['monitoring_flag'] = False
    BOT.send_message(message.chat.id, 'Авторизация чата отключена')
    sleep(7)
    CUR_PARAMS.pop(message.chat.id, None)  # Освобождаем в памяти словарь чата


@BOT.message_handler(commands=['game_info'])
def game_info(message):
    game_link = f'https://{CUR_PARAMS[message.chat.id].get("cur_domain", "")}/GameDetails.aspx?gid={CUR_PARAMS[message.chat.id]["cur_json"]["GameId"]}'
    game_doc = CUR_PARAMS[message.chat.id].get('doc', 'Не установлен')
    BOT.send_message(message.chat.id, f'Ссылка на игру: {game_link} \nСсылка на док: {game_doc} \n')


# список игроков для тегания например при АПе уровня
@BOT.message_handler(commands=['set_players'])
def set_players_list(message):
    cmd, *args = message.text.split()
    CUR_PARAMS[message.chat.id]['players'] = args
    BOT.send_message(message.chat.id, 'Список игроков установлен')


@BOT.message_handler(commands=['set_doc'])
def set_doc(message):
    doc_link = message.text.split()[1]
    CUR_PARAMS[message.chat.id]['doc'] = doc_link
    BOT.send_message(message.chat.id, 'Ссылка на док установлена')


@BOT.message_handler(commands=['set_coords'])
def set_coords(message):
    spl_msg = message.text.split()
    if len(spl_msg) == 1:
        BOT.send_message(message.chat.id, f'Текущие координаты: {CUR_PARAMS[message.chat.id]["last_coords"]}')
    if len(spl_msg) == 3:
        CUR_PARAMS[message.chat.id]["last_coords"] = (spl_msg[1], spl_msg[2])
        BOT.send_message(message.chat.id, f'Установлены новые текущие координаты: {CUR_PARAMS[message.chat.id]["last_coords"]}')


@BOT.message_handler(commands=['get_chat_id'])
def get_chat_id(message):
    BOT.send_message(message.chat.id, f'<code>{str(message.chat.id)}</code>', parse_mode='HTML')


@BOT.message_handler(commands=['game_monitor'])
def game_monitor(message):
    if len(message.text.split()) == 2 and message.text.split()[1] == '0':
        CUR_PARAMS[message.chat.id]['monitoring_flag'] = False
        sleep(7)
    else:
        if not (CUR_PARAMS[message.chat.id]['monitoring_flag']):
            CUR_PARAMS[message.chat.id]['monitoring_flag'] = True
            threading.Thread(target=monitoring_func(message.chat.id)).start()
        else:
            BOT.send_message(message.chat.id, 'Слежение уже запущено')


@BOT.message_handler(commands=['accept_codes', 'sector_monitor', 'bonus_monitor', 'route_builder'])
def switch_flag(message):
    d = {'accept_codes': 'Прием кодов',
         'sector_monitor': 'Мониторинг секторов',
         'bonus_monitor': 'Мониторинг бонусов',
         'route_builder': 'Построитель маршрутов'}
    cmd = message.text[1:].split()[0].split('@')[0].lower()
    if len(message.text.split()) == 2 and message.text.split()[1] == '0':
        cmd_flag = False
    else:
        cmd_flag = True
    CUR_PARAMS[message.chat.id][cmd] = cmd_flag
    BOT.send_message(message.chat.id, f'{d.get(cmd)} {"включен" if cmd_flag else "выключен"}')


@BOT.message_handler(commands=['time'])
def get_time(message):
    try:
        game_json = CUR_PARAMS[message.chat.id]['session'].get(f'https://{CUR_PARAMS[message.chat.id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message.chat.id]["cur_json"]["GameId"]}?json=1').json()
    except:
        BOT.send_message(message.chat.id, 'Ошибка, возможно необходимо заново авторизоваться')
        return

    if game_json['Event'] != 0:
        BOT.send_message(message.chat.id, 'Ошибка')
        return
    if game_json["Level"]["Timeout"] == 0:
        BOT.send_message(message.chat.id, f'Автопереход отсутствует')
        return
    BOT.send_message(message.chat.id, f'Автопереход через {datetime.timedelta(seconds=game_json["Level"]["TimeoutSecondsRemain"])}')


@BOT.message_handler(commands=['sectors', 'sectors_left'])
def get_sectors(message):
    # Если указан номер уровня, то загружаем из OLD_LEVELS
    cmd = message.text[1:].split()[0].split('@')[0].lower()
    if len(message.text.split()) == 2:
        if message.text.split()[1] in CUR_PARAMS[message.chat.id]['OLD_LEVELS']:
            game_json = CUR_PARAMS[message.chat.id]['OLD_LEVELS'][message.text.split()[1]]
        else:
            BOT.send_message(message.chat.id, 'Уровень не найден в прошедших')
            return
    else:
        try:
            game_json = CUR_PARAMS[message.chat.id]['session'].get(f'https://{CUR_PARAMS[message.chat.id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message.chat.id]["cur_json"]["GameId"]}?json=1').json()
        except:
            BOT.send_message(message.chat.id, 'Ошибка, возможно необходимо заново авторизоваться')
            return

    result_str = ''

    if game_json['Event'] != 0:
        BOT.send_message(message.chat.id, 'Ошибка')
        return

    for elem in game_json['Level']['Sectors']:
        if elem['IsAnswered']:
            if cmd == 'sectors':
                result_str += f'✅№{elem["Order"]} {elem["Name"]} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]}) {CUR_PARAMS[message.chat.id]["sector_closers"].get(elem["SectorId"], "")}\n'
        else:
            result_str += f'❌№{elem["Order"]} {elem["Name"]}\n'
    if result_str == '':
        result_str = 'Нет секторов'

    result_str = f'Осталось закрыть: {game_json["Level"]["SectorsLeftToClose"] if game_json["Level"]["SectorsLeftToClose"] > 0 else 1} из {len(game_json["Level"]["Sectors"]) if len(game_json["Level"]["Sectors"]) > 0 else 1}\n' + result_str

    for i in range(0, len(result_str), TASK_MAX_LEN):
        BOT.send_message(message.chat.id, result_str[i:i + TASK_MAX_LEN])


@BOT.message_handler(commands=['bonuses'])
def get_bonuses(message):
    if len(message.text.split()) == 2:
        if message.text.split()[1] in CUR_PARAMS[message.chat.id]['OLD_LEVELS']:
            game_json = CUR_PARAMS[message.chat.id]['OLD_LEVELS'][message.text.split()[1]]
        else:
            BOT.send_message(message.chat.id, 'Уровень не найден в прошедших')
            return
    else:
        try:
            game_json = CUR_PARAMS[message.chat.id]['session'].get(f'https://{CUR_PARAMS[message.chat.id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message.chat.id]["cur_json"]["GameId"]}?json=1').json()
        except:
            BOT.send_message(message.chat.id, 'Ошибка, возможно необходимо заново авторизоваться')
            return

    result_str = ''

    if game_json['Event'] != 0:
        BOT.send_message(message.chat.id, 'Ошибка')
        return

    for elem in game_json['Level']['Bonuses']:
        if elem['IsAnswered']:
            result_str += f'{"🔴" if elem["Negative"] else "🟢"}№{elem["Number"]} {elem["Name"] or ""} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]}) {CUR_PARAMS[message.chat.id]["bonus_closers"].get(elem["BonusId"], "")} {"Штраф: " if elem["Negative"] else "Бонус: "} {datetime.timedelta(seconds=elem["AwardTime"])}\n'
        else:
            result_str += f'{"✖Истёк" if elem["Expired"] else "❌"}№{elem["Number"]} {elem["Name"] or ""} {"Будет доступен через "+str(datetime.timedelta(seconds=elem["SecondsToStart"])) if elem["SecondsToStart"] != 0 else ""} {"Осталось на выполнение: "+str(datetime.timedelta(seconds=elem["SecondsLeft"])) if elem["SecondsLeft"] != 0 else ""}\n'
    if result_str == '':
        result_str = 'Нет бонусов'

    for i in range(0, len(result_str), TASK_MAX_LEN):
        BOT.send_message(message.chat.id, result_str[i:i + TASK_MAX_LEN])


@BOT.message_handler(commands=['hints'])
def get_hints(message):
    result_str = ''
    try:
        game_json = CUR_PARAMS[message.chat.id]['session'].get(f'https://{CUR_PARAMS[message.chat.id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message.chat.id]["cur_json"]["GameId"]}?json=1').json()
    except:
        BOT.send_message(message.chat.id, 'Ошибка, возможно необходимо заново авторизоваться')
        return

    if game_json['Event'] != 0:
        BOT.send_message(message.chat.id, 'Ошибка')
        return

    for elem in game_json['Level']['Helps']:
        if elem['RemainSeconds'] == 0:
            result_str += f'Подсказка {elem["Number"]}:\n{elem["HelpText"]}\n{"_"*30}\n\n'
        else:
            result_str += f'Подсказка {elem["Number"]}: Будет через {datetime.timedelta(seconds=elem["RemainSeconds"])}\n{"_"*30}\n\n'
    if result_str == '':
        result_str = 'Нет подсказок'
    #BOT.send_message(message.chat.id, result_str)
    BOT.send_message(message.chat.id, add_coords_copy(result_str), parse_mode='MarkDown')


@BOT.message_handler(commands=['task'])
def get_task(message):
    check_engine(message.chat.id)
    send_curlevel_info(message.chat.id, CUR_PARAMS[message.chat.id]['cur_json'])
    get_hints(message)


@BOT.message_handler(commands=['screen'])
def get_screen(message):
    if CUR_PARAMS[message.chat.id]['driver']:
        CUR_PARAMS[message.chat.id]['driver'].get(f'https://{CUR_PARAMS[message.chat.id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message.chat.id]["cur_json"]["GameId"]}?lang={LANG}')
        BOT.send_photo(message.chat.id, base64.b64decode(CUR_PARAMS[message.chat.id]['driver'].get_full_page_screenshot_as_base64()))
    else:
        BOT.send_message(message.chat.id, 'Виртуальный браузер не запущен')


@BOT.message_handler(commands=['open_browser'])
def start_browser(message):
    my_options = Options()
    my_options.set_preference("general.useragent.override", USER_AGENT['User-agent'])
    my_driver = webdriver.Firefox(options=my_options)
    my_driver.get(f'https://{CUR_PARAMS[message.chat.id]["cur_domain"]}')
    my_driver.add_cookie({'name': 'atoken', 'value': CUR_PARAMS[message.chat.id]['session'].cookies.get_dict()['atoken'], 'domain': '.en.cx', 'secure': False, 'httpOnly': True, 'session': True})
    my_driver.add_cookie({'name': 'stoken', 'value': CUR_PARAMS[message.chat.id]['session'].cookies.get_dict()['stoken'], 'domain': '.' + CUR_PARAMS[message.chat.id]['cur_domain'], 'secure': False, 'httpOnly': False, 'session': True})
    my_driver.get(f'https://{CUR_PARAMS[message.chat.id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message.chat.id]["cur_json"]["GameId"]}')


@BOT.message_handler(commands=['leave_chat'])
def leave_chat(message):
    chat_id = message.text.split()[1]
    BOT.leave_chat(chat_id)


# Обработка ошибок, которые фильтрует middleware_handler
@BOT.message_handler(commands=['incorrect_chat', 'incorrect_user'])
def send_error(message):
    match message.text:
        case '/incorrect_chat':
            BOT.send_message(message.chat.id, 'Команда доступна только в авторизованном чате')
        case '/incorrect_user':
            BOT.send_message(message.chat.id, 'Нет прав на данную команду')


@BOT.message_handler(commands=['load_old_json'])
def load_old_json(message):
    json_filename = str(message.chat.id) + '.' + str(CUR_PARAMS[message.chat.id]["cur_json"]["GameId"])
    if os.path.isfile('level_snapshots/'+json_filename):
        with open('level_snapshots/'+json_filename, 'r') as json_file:
            CUR_PARAMS[message.chat.id]['OLD_LEVELS'].update(json.load(json_file))
    else:
        BOT.send_message(message.chat.id, 'Файл не существует')


@BOT.message_handler(commands=['*', 'geo'])
def send_geo(message):
    input_lst = message.text.replace(',', ' ').split()
    if len(input_lst) == 3:
        BOT.send_location(message.chat.id, input_lst[1], input_lst[2])


@BOT.message_handler(func=lambda message: True)
def send_answer(message):
    if message.text[0] != '/':
        return
    if not (CUR_PARAMS[message.chat.id]['accept_codes']):
        BOT.send_message(message.chat.id, 'Прием кодов выключен! Для включения выполните /accept_codes')
        return

    sectors_list = []
    bonus_list = []
    answer = message.text[2:] if (message.text[1] == '!' and CUR_PARAMS[message.chat.id]['cur_json']['Level']['HasAnswerBlockRule']) else message.text[1:]

    # Если блокировка, нет бонусов и ответ не с !:
    if CUR_PARAMS[message.chat.id]['cur_json']['Level']['HasAnswerBlockRule'] and (len(CUR_PARAMS[message.chat.id]["cur_json"]["Level"]["Bonuses"]) == 0) and message.text[1] != '!':
        BOT.send_message(message.chat.id, 'На уровне блокировка, в сектор вбивайте самостоятельно или через /!')
        return

    # По умолчанию вбивать в бонус при блокировке, если ответ без !
    if CUR_PARAMS[message.chat.id]['cur_json']['Level']['HasAnswerBlockRule'] and message.text[1] != '!':
        answer_type = 'BonusAction'
        BOT.send_message(message.chat.id, 'На уровне блокировка, вбиваю в бонус, в сектор вбивайте самостоятельно или через /!')
    else:
        answer_type = 'LevelAction'

    try:
        old_json = CUR_PARAMS[message.chat.id]["session"].get(f'https://{CUR_PARAMS[message.chat.id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message.chat.id]["cur_json"]["GameId"]}?json=1').json()
        answer_json = CUR_PARAMS[message.chat.id]['session'].post(f'https://{CUR_PARAMS[message.chat.id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message.chat.id]["cur_json"]["GameId"]}?json=1', data={
            'LevelId': CUR_PARAMS[message.chat.id]["cur_json"]['Level']['LevelId'],
            'LevelNumber': CUR_PARAMS[message.chat.id]["cur_json"]['Level']['Number'],
            answer_type + '.answer': answer}).json()
    except:
        BOT.send_message(message.chat.id, 'Ошибка, возможно необходимо заново авторизоваться')
        return

    if answer_json['Event'] != 0:
        check_engine(message.chat.id)
        return

    if answer_json['EngineAction'][answer_type]['IsCorrectAnswer']:
        if answer_type == 'LevelAction':
            for elem in answer_json['Level']['Sectors']:
                if elem['IsAnswered'] and elem["Answer"]["Answer"].lower() == answer.lower():
                    if elem in old_json['Level']['Sectors']:
                        sectors_list.append(f'⚪Баян! Сектор №{elem["Order"]} {elem["Name"] or ""}')
                    else:
                        sectors_list.append(f'🟢Сектор №{elem["Order"]} {elem["Name"] or ""} закрыт!')
                        CUR_PARAMS[message.chat.id]['sector_closers'][elem["SectorId"]] = message.from_user.username

        for elem in answer_json['Level']['Bonuses']:
            if elem['IsAnswered'] and elem["Answer"]["Answer"].lower() == answer.lower():
                if elem in old_json['Level']['Bonuses']:
                    bonus_list.append(f'⚪Баян! Бонус №{elem["Number"]} {elem["Name"] or ""}\n{("Штрафное время: " if elem["Negative"] else "Бонусное время: ") + str(datetime.timedelta(seconds=elem["AwardTime"])) if elem["AwardTime"] != 0 else ""}\n{"Подсказка бонуса:" + chr(10) + elem["Help"] if elem["Help"] else ""}')
                else:
                    bonus_list.append(f'Бонус №{elem["Number"]} {elem["Name"] or ""} закрыт\n{("🔴 Штрафное время: " if elem["Negative"] else "🟢 Бонусное время: ") + str(datetime.timedelta(seconds=elem["AwardTime"])) if elem["AwardTime"] != 0 else ""}\n{"Подсказка бонуса:" + chr(10) + elem["Help"] if elem["Help"] else ""}')
                    CUR_PARAMS[message.chat.id]['bonus_closers'][elem["BonusId"]] = message.from_user.username
        result_str = '✅Ответ верный\n'+'\n'.join(sectors_list)+'\n'+'\n'.join(bonus_list)

        BOT.reply_to(message, result_str)
    elif answer_json['EngineAction'][answer_type]['IsCorrectAnswer'] is False:
        BOT.reply_to(message, '❌Ответ неверный')
    elif answer_json['EngineAction'][answer_type]['IsCorrectAnswer'] is None:
        BOT.reply_to(message, '❓Ответа не было, возможно поле заблокировано')
    check_engine(message.chat.id)


if __name__ == '__main__':
    print('Bot is running')
    BOT.infinity_polling()
