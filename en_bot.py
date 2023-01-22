import datetime
import json
from time import sleep
import requests
import telebot
import threading
import base64
from selenium import webdriver
from selenium.webdriver.firefox.options import Options  # Нужен еще установленный в системе firefox
import io
import re
import os.path
import configparser
# import curlify
# import webbrowser

# Читаем конфиг
config = configparser.ConfigParser()
config.read('settings.ini')
ADMIN_USERNAMES = tuple(config['Settings']['Admins'].split(','))  # Администраторы, которым разрешена авторизация бота в чате
SECTORS_LEFT_ALERT = int(config['Settings']['Sectors_left_alert']) # Количество оставшихся для закрытия секторов, с которого выводить оповещение, сколько осталось
USER_AGENT = {'User-agent': config['Settings']['User_agent']} # Выставляемый в requests и selenium user-agent
TASK_MAX_LEN = int(config['Settings']['Task_max_len']) # Максимальное кол-во символов в одном сообщении, если превышает, то разбивается на несколько

CUR_PARAMS = {}                 # словарь с текущими состояниями слежения в чатах
telebot.apihelper.ENABLE_MIDDLEWARE = True  # Разрешаем MIDDLEWARE до создания бота
BOT = telebot.TeleBot(config['Settings']['Token'], num_threads=int(config['Settings']['Threads'])) # еще вариант с потоками threaded=False


# Предварительная обработка команд
@BOT.middleware_handler(update_types=['message'])
def modify_message(bot_instance, message):
    if message.text is None:
        return
    cmd = message.text.split('@')[0].split()[0].lower()[1:]
    # Запрет всех команд в чате, кроме тех, которые могут работать в неавторизованном чате, перенаправляем на handler INCORRECT_CHAT
    if cmd not in ('help', 'start', 'auth', 'get_chat_id', '*', 'geo', 'leave_chat') and message.chat.id not in CUR_PARAMS:
        message.text = '/incorrect_chat'
        return
    # Запрет авторизации и загрузки из файла от всех, кроме админов, перенаправляем INCORRECT_USER
    if cmd in ('auth', 'stop_auth', 'load_old_json', 'open_browser', 'leave_chat') and message.from_user.username not in ADMIN_USERNAMES:
        message.text = '/incorrect_user'
        return


# Парсинг текста на список координат и файл KML
def gen_kml2(text):
    coord_list = re.findall(r'-?\d{1,2}\.\d{3,10}[, ]*-?\d{1,3}\.\d{3,10}', text)
    if not coord_list:
        return
    result_list = []
    kml = '<kml><Document>'
    for cnt, elem in enumerate(coord_list):
        c = re.findall(r'-?\d{1,3}\.\d{3,10}', elem)
        kml += f'<Placemark><name>Point {cnt+1}</name><Point><coordinates>{c[1]},{c[0]},0.0</coordinates></Point></Placemark>'
        result_list.append((c[0], c[1]))
    kml += '</Document></kml>'
    buf_file = io.StringIO()
    buf_file.write(kml)
    buf_file.seek(0, 0)
    return buf_file, result_list  # Возвращаем кортеж из файла kml и списка координат


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


# Отправить информацию о текущем уровне
def send_curlevel_info(cur_chat, cur_json):
    # Выводим информацию о номере уровня, автопереходе, блокировне ответов
    gameinfo_str = f'Уровень {cur_json["Level"]["Number"]} из {len(cur_json["Levels"])} {cur_json["Level"]["Name"]}\n'
    if cur_json["Level"]["Timeout"] > 0:
        gameinfo_str += f'Автопереход через {datetime.timedelta(seconds=cur_json["Level"]["Timeout"])}\n'
    else:
        gameinfo_str += 'Автопереход отсутствует\n'
    if cur_json["Level"]["HasAnswerBlockRule"]:
        gameinfo_str += f'ВНИМАНИЕ, БЛОКИРОВКА ОТВЕТОВ! НЕ БОЛЕЕ {cur_json["Level"]["AttemtsNumber"]} ПОПЫТОК ЗА {datetime.timedelta(seconds=cur_json["Level"]["AttemtsPeriod"])} ДЛЯ {"КОМАНДЫ" if cur_json["Level"]["BlockTargetId"] == 2 else "ИГРОКА"}'
    BOT.send_message(cur_chat, gameinfo_str)

    # Отдельно выводим задание
    if len(cur_json['Level']['Tasks']) > 0:
        gamelevel_str = cur_json['Level']['Tasks'][0]['TaskText']
        send_kml_info(cur_chat, gamelevel_str, cur_json["Level"]["Number"])
    else:
        gamelevel_str = 'Нет заданий на уровне'

    # Если очень большой текст на уровне, то сплит
    for i in range(0, len(gamelevel_str), TASK_MAX_LEN):
        BOT.send_message(cur_chat, gamelevel_str[i:i + TASK_MAX_LEN])


def check_engine(cur_chat_id):
    try:
        game_json = CUR_PARAMS[cur_chat_id]["session"].get(f'https://{CUR_PARAMS[cur_chat_id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[cur_chat_id]["cur_json"]["GameId"]}?json=1').json()
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
            return True  # игра еще не началась, продолжаем мониторить
        case 6 | 17:
            BOT.send_message(cur_chat_id, 'Игра закончилась')
            CUR_PARAMS[cur_chat_id]['monitoring_flag'] = False
            sleep(7)
            BOT.send_message(cur_chat_id, 'Авторизация чата отключена')
            CUR_PARAMS.pop(cur_chat_id, None)  # Освобождаем в памяти словарь чата
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
                send_curlevel_info(cur_chat_id, CUR_PARAMS[cur_chat_id]['cur_json'])
                return True

            # Проверка, что поменялся номер уровня, т.е. произошел АП
            if old_json['Level']['Number'] != CUR_PARAMS[cur_chat_id]['cur_json']['Level']['Number']:
                CUR_PARAMS[cur_chat_id]['5_min_sent'] = False
                CUR_PARAMS[cur_chat_id]['1_min_sent'] = False
                BOT.send_message(cur_chat_id, 'АП!\n' + ' '.join(CUR_PARAMS[cur_chat_id].get('players', '')))
                send_curlevel_info(cur_chat_id, CUR_PARAMS[cur_chat_id]['cur_json'])

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
            if old_json['Level']['Tasks'] != CUR_PARAMS[cur_chat_id]['cur_json']['Level']['Tasks']:
                BOT.send_message(cur_chat_id, 'Задание уровня изменилось')

            # проверка на сообщения на уровне:
            for elem in CUR_PARAMS[cur_chat_id]['cur_json']['Level']['Messages']:
                if elem not in old_json['Level']['Messages']:
                    BOT.send_message(cur_chat_id, f'Добавлено сообщение: {elem["MessageText"]}')

            # проверка на количество секторов на уровне:
            if len(old_json['Level']['Sectors']) != len(CUR_PARAMS[cur_chat_id]['cur_json']['Level']['Sectors']):
                BOT.send_message(cur_chat_id, 'Количество секторов на уровне изменилось')

            # проверка на количество бонусов на уровне:
            if len(old_json['Level']['Bonuses']) != len(CUR_PARAMS[cur_chat_id]['cur_json']['Level']['Bonuses']):
                BOT.send_message(cur_chat_id, 'Количество бонусов на уровне изменилось')

            # проверка на количество необходимых секторов:
            if old_json['Level']['RequiredSectorsCount'] != CUR_PARAMS[cur_chat_id]['cur_json']['Level']['RequiredSectorsCount']:
                BOT.send_message(cur_chat_id, 'Количество необходимых для прохождения секторов изменилось')

            # проверка на кол-во оставшихся секторов:
            cur_sectors_left = CUR_PARAMS[cur_chat_id]['cur_json']['Level']['SectorsLeftToClose']
            if old_json['Level']['SectorsLeftToClose'] != cur_sectors_left and cur_sectors_left <= SECTORS_LEFT_ALERT:
                sector_list = [str(elem['Order']) for elem in CUR_PARAMS[cur_chat_id]['cur_json']['Level']['Sectors'] if not (elem['IsAnswered'])]
                BOT.send_message(cur_chat_id, f'Осталось секторов: [{cur_sectors_left}]. Оставшиеся: {", ".join(sector_list)}')

            # Проверка, что пришла подсказка
            if len(CUR_PARAMS[cur_chat_id]["cur_json"]['Level']['Helps']) != len(old_json['Level']['Helps']):
                BOT.send_message(cur_chat_id, 'Была добавлена подсказка')
            else:
                for i, elem in enumerate(CUR_PARAMS[cur_chat_id]["cur_json"]['Level']['Helps']):
                    if elem['HelpText'] != old_json['Level']['Helps'][i]['HelpText']:
                        BOT.send_message(cur_chat_id, f'Подсказка {i + 1}: {elem["HelpText"]}')
                        send_kml_info(cur_chat_id, elem["HelpText"], f'{CUR_PARAMS[cur_chat_id]["cur_json"]["Level"]["Number"]}_{i+1}')

            # мониторинг закрытия секторов
            if CUR_PARAMS[cur_chat_id]['sector_monitor']:
                for elem in CUR_PARAMS[cur_chat_id]['cur_json']['Level']['Sectors']:
                    if elem not in old_json['Level']['Sectors'] and elem["IsAnswered"]:
                        BOT.send_message(cur_chat_id, f'✅№{elem["Order"]} {elem["Name"]} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]})\n')

            # мониторинг закрытия бонусов
            if CUR_PARAMS[cur_chat_id]['bonus_monitor']:
                for elem in CUR_PARAMS[cur_chat_id]['cur_json']['Level']['Bonuses']:
                    if elem not in old_json['Level']['Bonuses'] and elem["IsAnswered"]:
                        BOT.send_message(cur_chat_id, f'{"🔴" if elem["Negative"] else "🟢"} №{elem["Number"]} {elem["Name"] or ""} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]}) {"Штраф: " if elem["Negative"] else "Бонус: "} {datetime.timedelta(seconds=elem["AwardTime"])}\n{"Подсказка бонуса:"+ chr(10) + elem["Help"] if elem["Help"] else ""}')
                        if elem["Help"]:
                            send_kml_info(cur_chat_id, elem["Help"], CUR_PARAMS[cur_chat_id]["cur_json"]["Level"]["Number"])

            # мониторинг времени до автоперехода
            if 300 > CUR_PARAMS[cur_chat_id]['cur_json']['Level']['TimeoutSecondsRemain'] > 0 and not(CUR_PARAMS[cur_chat_id]['5_min_sent']):
                BOT.send_message(cur_chat_id, 'До автоперехода осталось менее 5 минут!')
                CUR_PARAMS[cur_chat_id]['5_min_sent'] = True
            if 60 > CUR_PARAMS[cur_chat_id]['cur_json']['Level']['TimeoutSecondsRemain'] > 0 and not(CUR_PARAMS[cur_chat_id]['1_min_sent']):
                BOT.send_message(cur_chat_id, 'До автоперехода осталось менее 1 минуты!')
                CUR_PARAMS[cur_chat_id]['1_min_sent'] = True
    return True


def monitoring_func(cur_chat_id):
    start_time = datetime.datetime.now()
    BOT.send_message(cur_chat_id, 'Мониторинг включен')
    while CUR_PARAMS[cur_chat_id]['monitoring_flag']:
        print(f'Слежение за игрой в чате {cur_chat_id} работает {datetime.datetime.now()-start_time}')
        sleep(6)
        try:
            if not(check_engine(cur_chat_id)):
                break
        except:
            print('Ошибка функции check_engine, продолжаю мониторинг')
    BOT.send_message(cur_chat_id, 'Мониторинг выключен')


@BOT.message_handler(commands=['help', 'start'])
def send_welcome(message):
    BOT.send_message(message.chat.id, '''Temig enbot v1.0
/help - этот help
/auth домен id_игры логин пароль [id_чата] - авторизовать бота на игру в игровом чате
/stop_auth - отключить чат
/get_chat_id - получить id чата
/game_monitor [0] - включить\[отключить] слежение за игрой
/sectors [level№] - показать сектора [прошедшего_уровня]
/bonuses [level№] - показать бонусы [прошедшего_уровня]
/hints - показать подсказки
/task - показать текунее задание
/screen - скриншот текущего уровня (необходим firefox)
/ - отправка кода после /
/accept_codes [0] - включить\[выключить] прием кодов из чата
/sector_monitor [0] - включить\[выключить] мониторинг секторов
/bonus_monitor [0] - включить\[выключить] мониторинг бонусов
/time - оставшееся время до апа
/load_old_json - загрузить информацию о прошедших уровнях игры из файла (при перезапуске бота)
/geo или /* координаты через пробел - отправить геометку по координатам 
/set_players @игрок1 @игрок2 - установить список полевых игроков
/open_browser открыть бразуер на компьютере, где запущен бот, привязанный к сессии бота (необходим firefox)
/leave_chat id_чата - покинуть чат с указанным id
''')



@BOT.message_handler(commands=['auth'])
def auth(message):
    # Проверка на пользователя, у кого есть права на авторизацию бота осуществляется в middleware handler
    input_list = message.text.split()

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
                '5_min_sent': False,
                '1_min_sent': False,
                'OLD_LEVELS': {},
                'driver': None}

            # запускаем firefox браузер, который будем использовать для скриншотов
            options = Options()
            options.add_argument("--headless")
            options.set_preference("general.useragent.override", USER_AGENT['User-agent'])
            my_driver = webdriver.Firefox(options=options)
            my_driver.get(f'https://{my_domain}')
            my_driver.add_cookie({'name': 'atoken', 'value': my_session.cookies.get_dict()['atoken'], 'domain': '.en.cx', 'secure': False, 'httpOnly': True, 'session': True})
            my_driver.add_cookie({'name': 'stoken', 'value': my_session.cookies.get_dict()['stoken'], 'domain': '.' + my_domain, 'secure': False, 'httpOnly': False, 'session': True})
            CUR_PARAMS[cur_chat_id]['driver'] = my_driver
            # CUR_PARAMS[cur_chat_id]['driver'].add_cookie({'name': 'GUID', 'value': CUR_PARAMS[cur_chat_id]['session'].cookies.get_dict()['GUID'], 'domain': CUR_PARAMS[cur_chat_id]['cur_domain'], 'secure': False, 'httpOnly': True, 'session': False})
            # r = CUR_PARAMS[cur_chat_id]['session'].get(f'https://{CUR_PARAMS[cur_chat_id]["cur_domain"]}/GameEngines/Encounter/Play/{my_game_id}')
            # print(curlify.to_curl(r.request))


@BOT.message_handler(commands=['stop_auth'])
def stop_auth(message):
    CUR_PARAMS[message.chat.id]['monitoring_flag'] = False
    BOT.send_message(message.chat.id, 'Авторизация чата отключена')
    sleep(7)
    CUR_PARAMS.pop(message.chat.id, None)  # Освобождаем в памяти словарь чата


# список игроков для тегания например при АПе уровня
@BOT.message_handler(commands=['set_players'])
def set_players_list(message):
    cmd, *args = message.text.split()
    CUR_PARAMS[message.chat.id]['players'] = args
    BOT.send_message(message.chat.id, 'Список игроков установлен')


@BOT.message_handler(commands=['get_chat_id'])
def get_chat_id(message):
    BOT.send_message(message.chat.id, str(message.chat.id))


@BOT.message_handler(commands=['game_monitor'])
def game_monitor(message):
    if len(message.text.split()) == 2 and message.text.split()[1] == '0':
        CUR_PARAMS[message.chat.id]['monitoring_flag'] = False
        sleep(7)
    else:
        if not(CUR_PARAMS[message.chat.id]['monitoring_flag']):
            CUR_PARAMS[message.chat.id]['monitoring_flag'] = True
            threading.Thread(target=monitoring_func(message.chat.id)).start()
        else:
            BOT.send_message(message.chat.id, 'Слежение уже запущено')


@BOT.message_handler(commands=['accept_codes'])
def accept_codes(message):
    if len(message.text.split()) == 2 and message.text.split()[1] == '0':
        CUR_PARAMS[message.chat.id]['accept_codes'] = False
        BOT.send_message(message.chat.id, f'Прием кодов выключен')
    else:
        CUR_PARAMS[message.chat.id]['accept_codes'] = True
        BOT.send_message(message.chat.id, f'Прием кодов включен')


@BOT.message_handler(commands=['sector_monitor'])
def sector_monitor(message):
    if len(message.text.split()) == 2 and message.text.split()[1] == '0':
        CUR_PARAMS[message.chat.id]['sector_monitor'] = False
        BOT.send_message(message.chat.id, f'Мониторинг секторов выключен')
    else:
        CUR_PARAMS[message.chat.id]['sector_monitor'] = True
        BOT.send_message(message.chat.id, f'Мониторинг секторов включен')


@BOT.message_handler(commands=['bonus_monitor'])
def bonus_monitor(message):
    if len(message.text.split()) == 2 and message.text.split()[1] == '0':
        CUR_PARAMS[message.chat.id]['bonus_monitor'] = False
        BOT.send_message(message.chat.id, f'Мониторинг бонусов выключен')
    else:
        CUR_PARAMS[message.chat.id]['bonus_monitor'] = True
        BOT.send_message(message.chat.id, f'Мониторинг бонусов включен')


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
    BOT.send_message(message.chat.id, f'Автопереход через {datetime.timedelta(seconds=game_json["Level"]["TimeoutSecondsRemain"])}')


@BOT.message_handler(commands=['sectors'])
def get_sectors(message):
    # Если указан номер уровня, то загружаем из OLD_LEVELS
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
            result_str += f'✅№{elem["Order"]} {elem["Name"]} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]})\n'
        else:
            result_str += f'❌№{elem["Order"]} {elem["Name"]}\n'
    if result_str == '':
        result_str = 'Нет секторов'
    BOT.send_message(message.chat.id, result_str)


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
            result_str += f'{"🔴" if elem["Negative"] else "🟢"}№{elem["Number"]} {elem["Name"] or ""} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]}) {"Штраф: " if elem["Negative"] else "Бонус: "} {datetime.timedelta(seconds=elem["AwardTime"])}\n'
        else:
            result_str += f'{"✖Истёк" if elem["Expired"] else "❌"}№{elem["Number"]} {elem["Name"] or ""} {"Будет доступен через "+str(datetime.timedelta(seconds=elem["SecondsToStart"])) if elem["SecondsToStart"]!=0 else""} {"Осталось на выполнение: "+str(datetime.timedelta(seconds=elem["SecondsLeft"])) if elem["SecondsLeft"]!=0 else""}\n'
    if result_str == '':
        result_str = 'Нет бонусов'
    BOT.send_message(message.chat.id, result_str)


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
    BOT.send_message(message.chat.id, result_str)


@BOT.message_handler(commands=['task'])
def get_task(message):
    check_engine(message.chat.id)
    send_curlevel_info(message.chat.id, CUR_PARAMS[message.chat.id]['cur_json'])


@BOT.message_handler(commands=['screen'])
def get_screen(message):
    # start_time = datetime.datetime.now()
    # print(CUR_PARAMS[message.chat.id]['session'].cookies.get_dict())
    # CUR_PARAMS[message.chat.id]['driver'].add_cookie({'name': 'atoken', 'value': CUR_PARAMS[message.chat.id]['session'].cookies.get_dict()['atoken'], 'domain': '.en.cx', 'secure': False, 'httpOnly': True, 'session': True})
    # CUR_PARAMS[message.chat.id]['driver'].add_cookie({'name': 'stoken', 'value': CUR_PARAMS[message.chat.id]['session'].cookies.get_dict()['stoken'], 'domain': '.' + CUR_PARAMS[message.chat.id]['cur_domain'], 'secure': False, 'httpOnly': False, 'session': True})
    if CUR_PARAMS[message.chat.id]['driver']:
        CUR_PARAMS[message.chat.id]['driver'].get(f'https://{CUR_PARAMS[message.chat.id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message.chat.id]["cur_json"]["GameId"]}')
        BOT.send_photo(message.chat.id, base64.b64decode(CUR_PARAMS[message.chat.id]['driver'].get_full_page_screenshot_as_base64()))
        # print(f'Скриншот отправлен за {datetime.datetime.now()-start_time}')


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
    if not(CUR_PARAMS[message.chat.id]['accept_codes']):
        BOT.send_message(message.chat.id, 'Прием кодов выключен! Для включения выполните /accept_codes')
        return

    sectors_list = []
    bonus_list = []
    answer = message.text[2:] if message.text[1] == '!' else message.text[1:]

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
        answer_json = CUR_PARAMS[message.chat.id]['session'].post(f'https://{CUR_PARAMS[message.chat.id]["cur_domain"]}/GameEngines/Encounter/Play/{CUR_PARAMS[message.chat.id]["cur_json"]["GameId"]}?json=1', data={'LevelId': CUR_PARAMS[message.chat.id]["cur_json"]['Level']['LevelId'], 'LevelNumber': CUR_PARAMS[message.chat.id]["cur_json"]['Level']['Number'], answer_type + '.answer': answer}).json()
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
        for elem in answer_json['Level']['Bonuses']:
            if elem['IsAnswered'] and elem["Answer"]["Answer"].lower() == answer.lower():
                if elem in old_json['Level']['Bonuses']:
                    bonus_list.append(f'⚪Баян! Бонус №{elem["Number"]} {elem["Name"] or ""}\n{("Штрафное время: " if elem["Negative"] else "Бонусное время: ") + str(datetime.timedelta(seconds=elem["AwardTime"])) if elem["AwardTime"] !=0 else ""}\n{"Подсказка бонуса:"+ chr(10) + elem["Help"] if elem["Help"] else ""}')
                else:
                    bonus_list.append(f'Бонус №{elem["Number"]} {elem["Name"] or ""} закрыт\n{("🔴 Штрафное время: " if elem["Negative"] else "🟢 Бонусное время: ") + str(datetime.timedelta(seconds=elem["AwardTime"])) if elem["AwardTime"] != 0 else ""}\n{"Подсказка бонуса:"+ chr(10) + elem["Help"] if elem["Help"] else ""}')
        result_str = '✅Ответ верный\n'+'\n'.join(sectors_list)+'\n'+'\n'.join(bonus_list)
        BOT.reply_to(message, result_str)
    elif answer_json['EngineAction'][answer_type]['IsCorrectAnswer'] is False:
        BOT.reply_to(message, '❌Ответ неверный')
    elif answer_json['EngineAction'][answer_type]['IsCorrectAnswer'] is None:
        BOT.reply_to(message, '❓Ответа не было, возможно поле заблокировано')
    check_engine(message.chat.id)


BOT.infinity_polling()
