import configparser
import io
import base64
from bs4 import BeautifulSoup
import asyncio
import datetime
import aiohttp
import logging
import re
import os
import json
from typing import Awaitable, Callable, Any
import random
from playwright.async_api import async_playwright, Browser
from urllib.parse import urlparse, parse_qs, urlencode

EN_AUTH_ERRORS = {
    1: 'Превышено количество неправильных попыток авторизации',
    2: 'Неверный логин или пароль',
    3: 'Пользователь или в Cибири, или в черном списке, или на домене нельзя авторизовываться с других доменов',
    4: 'Блокировка по IP',
    5: 'В процессе авторизации произошла ошибка на сервере',
    6: 'Ошибка',
    7: 'Пользователь заблокирован администратором',
    8: 'Новый пользователь не активирован',
    9: 'Действия пользователя расценены как брутфорс',
    10: 'Пользователь не подтвердил e-mail',
    0: 'Авторизация успешна'
}

EN_EVENT_ERRORS = {
    2: 'Игра с указанным id не существует',
    4: 'Ошибка авторизации',
    5: 'Игра еще не началась',
    6: 'Игра закончилась',
    7: 'Заявка не подана',
    8: 'Заявка не подана',
    9: 'Команда не принята в игру',
    10: 'Аккаунт не в команде',
    11: 'Аккаунт не активен в команде',
    12: 'Игра не содержит уровней',
    13: 'Превышено количество участников',
    16: 'Уровень был снят',
    17: 'Игра закончилась',
    18: 'Уровень был снят',
    19: 'Уровень пройден по автопереходу',
    20: 'Все секторы выполнены',
    21: 'Уровень был снят',
    22: 'Уровень пройден по автопереходу',
    0: 'Игра в нормальном состоянии',
    99: 'Ошибка мониторинга бота'
}


def get_cookie(cookie_name: str, session: aiohttp.ClientSession) -> str:
    for cookie in session.cookie_jar:
        if cookie.key == cookie_name:
            return cookie.value


def parse_html(html_content: str, parse_flag: bool = True) -> str:
    if not parse_flag:
        return html_content
    try:
        soup = BeautifulSoup(html_content, 'lxml')
        for img_tag in soup.find_all('img'):
            src = img_tag.get('src')
            if src:
                inline_image_text = f"[Img: {src}]"
                img_tag.replace_with(inline_image_text + " ")
            else:
                img_tag.decompose()

        for br_tag in soup.find_all(['br', 'br/']):
            br_tag.replace_with('\n')

        for a_tag in soup.find_all('a'):
            href = a_tag.get('href')
            link_text = a_tag.get_text(strip=True)
            if href and link_text:
                inline_link_text = f"[{link_text}]({href})"
                a_tag.replace_with(inline_link_text)
            else:
                a_tag.replace_with(link_text)

        text_content = soup.get_text()
    except Exception as e:
        text_content = f'Ошибка парсинга текста: {e} \n {html_content}'

    return text_content


def get_yandex_constructor(script_html):
    match = re.search(r'src="(https://api-maps.yandex.ru/services/constructor.*?)"', script_html)
    if match:
        script_src_url = match.group(1)
        script_src_url = script_src_url.replace('&amp;', '&')
        parsed_url = urlparse(script_src_url)
        query_params = parse_qs(parsed_url.query)
        um_value = query_params.get('um', [None])[0]
        if um_value:
            yandex_maps_base_url = "https://yandex.ru/maps/"
            target_params = {
                "from": "mapframe",
                "source": "mapframe",
                "utm_source": "mapframe",
                "um": um_value
            }
            encoded_params = urlencode(target_params)
            final_yandex_maps_url = f"{yandex_maps_base_url}?{encoded_params}"
            return final_yandex_maps_url


def generate_kml(coord_list: list[list]) -> str:
    kml = '<kml><Document>'
    for elem in coord_list:
        kml += f'<Placemark><name>{elem[0]}</name><Point><coordinates>{elem[2]},{elem[1]},0.0</coordinates></Point></Placemark>'
    kml += '</Document></kml>'
    return kml


async def parse_yandex_constructor(url: str):
    headers = {"User-Agent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            html_content = await resp.read()
    soup = BeautifulSoup(html_content, 'lxml')
    script_tag = soup.find('script', {'type': 'application/json', 'class': 'state-view'})

    if script_tag:
        json_string = script_tag.string
        try:
            data = json.loads(json_string)
            named_coords = []
            for elem in data['config']['userMap']['features']:
                title = elem.get('title')
                subtitle = elem.get('subtitle')
                latitude = str(elem.get('coordinates', ['0.0', '0.0'])[1])[:7]
                longitude = str(elem.get('coordinates', ['0.0', '0.0'])[0])[:7]
                named_coords.append([f'{title}|{subtitle}', latitude, longitude])
            return named_coords
        except Exception as e:
            return None


async def gen_kml2(text: str) -> dict:
    numbered_coord_list = []
    buf_file = None
    buf_file_constr = None
    constr_named_coords = []

    raw_coord_pairs = re.findall(r'-?\d{1,2}\.\d{3,10}[, ]*-?\d{1,3}\.\d{3,10}', text)
    seen_coords = set()
    cnt = 0
    if raw_coord_pairs:
        for raw_pair_str in raw_coord_pairs:
            match = re.search(r'(-?\d{1,2}\.\d{3,10})[, ]*(-?\d{1,3}\.\d{3,10})', raw_pair_str)
            if match:
                lat, lon = match.groups()
                coord_tuple = (lat, lon)
                if coord_tuple not in seen_coords:
                    seen_coords.add(coord_tuple)
                    cnt += 1
                    numbered_coord_list.append([cnt, lat, lon])

        kml = generate_kml(numbered_coord_list)
        buf_file = io.BytesIO(kml.encode('utf-8'))
        buf_file.seek(0, 0)

    yandex_constructor_url = get_yandex_constructor(text)
    if yandex_constructor_url:
        constr_named_coords = await parse_yandex_constructor(yandex_constructor_url)
        constr_kml = generate_kml(constr_named_coords)
        buf_file_constr = io.BytesIO(constr_kml.encode('utf-8'))
        buf_file_constr.seek(0, 0)

    return {'kml_file': buf_file,
            'coords': numbered_coord_list,
            'yandex_constructor_url': yandex_constructor_url,
            'kml_file_constr': buf_file_constr,
            'coords_constr': constr_named_coords
            }


class EncounterBot:
    def __init__(self, message_func: Callable[[Any, [str | io.BytesIO | list[Any]]], Awaitable[None]], browser: Browser | None, globalconfig: dict):
        """ message_func - функция для отправки сообщений, которая будет вызываться в случае необходимости отправки сообщения в чат.
        Должна быть с двумя параметрами peer_id и message, возвращающая None. В ней самостоятельно реализовать отправку для разных типов мессенджеров
        Там же можно обработать сплит сообщений на несколько, если оно длинное и прочее. В message будет или текстовая строка, или файл в BytesIO (например для отправки kml файлов и скринов) или координаты"""
        self.message_func = message_func
        self.browser = browser
        self.globalconfig = globalconfig
        self.cur_chats = dict()

    # Создание класса вынесено в фабрику, т.к. нужно асинхронно создать один глобальный браузер для всех сессий
    @classmethod
    async def create(cls, message_func: Callable[[Any, [str | io.BytesIO | list[Any]]], Awaitable[None]]):
        config = configparser.ConfigParser()
        config.read('en_settings.ini', encoding='utf-8')
        globalconfig = dict()
        globalconfig['SECTORS_LEFT_ALERT'] = int(config['Settings']['Sectors_left_alert'])
        globalconfig['USER_AGENT'] = config['Settings']['User_agent']
        globalconfig['LANG'] = config['Settings']['Lang']
        globalconfig['CHECK_INTERVAL'] = int(config['Settings']['Check_interval'])
        globalconfig['TIMELEFT_ALERT1'] = int(config['Settings']['Timeleft_alert1'])
        globalconfig['TIMELEFT_ALERT2'] = int(config['Settings']['Timeleft_alert2'])
        globalconfig['STOP_ACCEPT_CODES_WORDS'] = tuple(config['Settings']['Stop_accept_codes_words'].split(','))
        globalconfig['USE_BROWSER'] = True if config['Settings']['Use_browser'].lower() == 'true' else False
        globalconfig['YANDEX_API_KEY'] = config['Settings']['Yandex_api_key']
        with open('yandex_api.txt', 'r', encoding='utf8') as yandex_api_file:
            globalconfig['YANDEX_API_PATTERN'] = yandex_api_file.read()
        globalconfig['MAP_TYPE'] = config['Settings']['Map_type']
        globalconfig['MAP_BROWSER_SLEEP'] = int(config['Settings']['Map_browser_sleep'])
        globalconfig['MAP_BROWSER_TIMEOUT'] = int(config['Settings']['Map_browser_timeout'])
        globalconfig['BROWSER_TYPE'] = config['Settings']['Browser_type']
        if globalconfig['USE_BROWSER']:
            p = await async_playwright().start()
            if globalconfig['BROWSER_TYPE'] == 'firefox':
                browser = await p.firefox.launch(headless=True)
            elif globalconfig['BROWSER_TYPE'] == 'chromium':
                browser = await p.chromium.launch(headless=True)
            else:
                browser = await p.firefox.launch(headless=True)  # по умолчанию firefox
            logging.info('Виртуальный браузер запущен')
        else:
            browser = None

        folder_path = os.path.join(os.curdir, 'level_snapshots')
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)

        return cls(message_func=message_func, browser=browser, globalconfig=globalconfig)

    async def close(self):
        if self.browser:
            await self.browser.close()

    async def send_kml_info(self, peer_id: str | int, text_to_parse: str, level_num: str | int) -> None:
        kml_parse = await gen_kml2(text_to_parse)
        kml_file = kml_parse.get('kml_file')

        if kml_file:
            coords_list = kml_parse.get('coords')
            kml_file.name = f'points{level_num}.kml'
            await self.message_func(peer_id, kml_file)
            await self.message_func(peer_id, coords_list[0][1:])

            # Построитель маршрутов
            chat_data = self.cur_chats.get(peer_id)
            if chat_data and chat_data['route_builder'] and chat_data['last_coords']:
                if routes := await self.get_route_screen_async(peer_id, chat_data['last_coords'], coords_list[0][1:]):
                    start_route, full_route = routes
                    start_route.name = f'start_route{level_num}.png'
                    full_route.name = f'full_route{level_num}.png'
                    await self.message_func(peer_id, start_route)
                    await self.message_func(peer_id, full_route)
            chat_data['last_coords'] = coords_list[0][1:]

        yandex_constr_url = kml_parse.get('yandex_constructor_url')
        if yandex_constr_url:
            await self.message_func(peer_id, f'Обнаружена ссылка на Яндекс конструктор: {yandex_constr_url}')
            kml_file_constr = kml_parse.get('kml_file_constr')
            if kml_file_constr:
                kml_file_constr.name = f'constr{level_num}.kml'
                await self.message_func(peer_id, kml_file_constr)
            coords_constr = kml_parse.get('coords_constr')
            if coords_constr:
                coord_str = ''
                for elem in coords_constr:
                    coord_str += f'{elem[0]} {elem[1]} {elem[2]}\n'
                await self.message_func(peer_id, coord_str)

    async def get_route_screen_async(self, peer_id: str | int, start_coords, end_coords) -> tuple[io.BytesIO, io.BytesIO] | None:
        if start_coords == end_coords:
            return
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            return
        if not (my_page := chat_data.get('browser', {}).get('page', None)):
            if context := chat_data.get('browser', {}).get('context', None):
                my_page = await context.new_page()
                chat_data['browser']['page'] = my_page
            else:
                return

        api_pattern = self.globalconfig['YANDEX_API_PATTERN']
        api_key = self.globalconfig['YANDEX_API_KEY']
        map_type = self.globalconfig['MAP_TYPE']
        browser_timeout = self.globalconfig['MAP_BROWSER_TIMEOUT']
        browser_sleep = self.globalconfig['MAP_BROWSER_SLEEP']

        html_bs64_1 = base64.b64encode(api_pattern.replace('#coords1', f'{start_coords[0]},{start_coords[1]}').replace('#coords2', f'{end_coords[0]}, {end_coords[1]}').replace('#my_api_key', api_key).replace('#bounds_flag', 'false').replace('#map_type', map_type).replace('loaded', 'loaded1').encode('utf-8')).decode()
        await my_page.goto('data:text/html;base64,' + html_bs64_1)
        try:
            await my_page.wait_for_function("document.title === 'loaded1'", timeout=browser_timeout * 1000)
        except TimeoutError:
            return
        await my_page.wait_for_timeout(browser_sleep * 1000)
        img_route_start = io.BytesIO(await my_page.screenshot(full_page=True, type='png'))

        html_bs64_2 = base64.b64encode(api_pattern.replace('#coords1', f'{start_coords[0]},{start_coords[1]}').replace('#coords2', f'{end_coords[0]}, {end_coords[1]}').replace('#my_api_key', api_key).replace('#bounds_flag', 'true').replace('#map_type', map_type).replace('loaded', 'loaded2').encode('utf-8')).decode()
        await my_page.goto('data:text/html;base64,' + html_bs64_2)
        try:
            await my_page.wait_for_function("document.title === 'loaded2'", timeout=browser_timeout * 1000)
        except TimeoutError:
            return
        await my_page.wait_for_timeout(browser_sleep * 1000)
        img_route_full = io.BytesIO(await my_page.screenshot(full_page=True, type='png'))

        return img_route_start, img_route_full

    async def set_coords(self, peer_id, coords: list[str, str] | tuple[str, str]):
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            await self.message_func(peer_id, 'Чат не авторизован')
            return
        if len(coords) != 2:
            return
        chat_data['last_coords'] = coords
        await self.message_func(peer_id, f'Координаты установлены: {coords[0], coords[1]}')

    # Получение скринов
    async def get_screen_as_bytes_async(self, peer_id: str | int, full: bool = False, w_article: str | None = None) -> io.BytesIO | None:
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            await self.message_func(peer_id, 'Чат не авторизован')
            return
        if not (my_page := chat_data.get('browser', {}).get('page', None)):
            if context := chat_data.get('browser', {}).get('context', None):
                my_page = await context.new_page()
                chat_data['browser']['page'] = my_page
            else:
                return
        if w_article:
            url = 'https://ru.wikipedia.org/wiki/'+w_article
        else:
            url = f'https://{self.cur_chats[peer_id]["cur_domain"]}/GameEngines/Encounter/Play/{self.cur_chats[peer_id]["cur_json"]["GameId"]}?lang={self.globalconfig['LANG']}'
        await my_page.goto(url, wait_until='networkidle', timeout=7000)

        css_h = await my_page.evaluate("() => document.documentElement.scrollHeight")
        dpr = await my_page.evaluate("() => window.devicePixelRatio || 1")
        pixel_h = int(css_h * dpr)
        if full:
            img_buffer = io.BytesIO(await my_page.screenshot(full_page=True, type='png'))
        else:
            img_buffer = io.BytesIO(await my_page.screenshot(full_page=False, type='png'))
            pixel_h = 683
        img_buffer.name = f'{pixel_h}_{w_article or "screen_file"}.png'
        return img_buffer

    # Возвращает страницы с информацией о текущем уровне. Перед вызовом нужно освежить текущий json
    async def get_curlevel_info(self, peer_id: str | int) -> tuple[str, str] | None:
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            await self.message_func(peer_id, 'Чат не авторизован')
            return
        cur_json = chat_data['cur_json']
        level = cur_json['Level']
        parser_flag = chat_data.get('parser', False)

        # Формируем инфу об уровне
        gameinfo_str = f'Уровень {level["Number"]} из {len(cur_json["Levels"])} {level["Name"]}\n'
        gameinfo_str += f'Выполнить секторов: {level["RequiredSectorsCount"] if level["RequiredSectorsCount"] > 0 else 1} из {len(level["Sectors"]) if len(level["Sectors"]) > 0 else 1}\n'
        if level["Messages"]:
            gameinfo_str += 'Сообщения на уровне:\n'
            for elem in level["Messages"]:
                gameinfo_str += elem["MessageText"] + '\n'
        if level["Timeout"] > 0:
            gameinfo_str += f'Автопереход через {datetime.timedelta(seconds=level["Timeout"])}\n'
        else:
            gameinfo_str += 'Автопереход отсутствует\n'
        if level["HasAnswerBlockRule"]:
            gameinfo_str += f'ВНИМАНИЕ, БЛОКИРОВКА ОТВЕТОВ! НЕ БОЛЕЕ {level["AttemtsNumber"]} ПОПЫТОК ЗА {datetime.timedelta(seconds=level["AttemtsPeriod"])} ДЛЯ {"КОМАНДЫ" if level["BlockTargetId"] == 2 else "ИГРОКА"}'
        gameinfo_str = parse_html(gameinfo_str, parser_flag)

        # Отдельно выводим задание
        if len(level['Tasks']) > 0:
            gamelevel_str = level['Tasks'][0]['TaskText']
        else:
            gamelevel_str = 'Нет заданий на уровне'
        gamelevel_str = parse_html(gamelevel_str, parser_flag)

        return gameinfo_str, gamelevel_str

    # Авторизация на движке
    async def auth(self, peer_id: str | int, domain: str, game_id: str | int, login: str, password: str) -> bool:
        if session := self.cur_chats.get(peer_id, {}).get('session'):
            await session.close()
        my_session = aiohttp.ClientSession(headers={"User-Agent": self.globalconfig['USER_AGENT']})
        try:
            async with my_session.post(f'https://{domain}/login/signin?json=1', data={'Login': login, 'Password': password}) as response:
                response.raise_for_status()
                auth_request_json = await response.json()
        except Exception as e:
            logging.error(f"Ошибка авторизации бота: {e}", exc_info=True)
            await self.message_func(peer_id, f'Ошибка запроса авторизации, возможно неверно указан домен:{e}')
            return False

        if auth_request_json['Error'] != 0:
            await self.message_func(peer_id, EN_AUTH_ERRORS.get(auth_request_json['Error'], 'Неизвестная ошибка'))
            return False

        logging.info('Авторизация успешна')
        await self.message_func(peer_id, 'Авторизация успешна')
        try:
            # Получаем информацию об игре
            async with my_session.get(f'https://{domain}/GameEngines/Encounter/Play/{game_id}?json=1') as response:
                response.raise_for_status()
                cur_json = await response.json()
        except Exception as e:
            logging.error(f"Ошибка авторизации бота: {e}", exc_info=True)
            await self.message_func(peer_id, f'Ошибка запроса авторизации, возможно неверно указан id игры: {e}')
            return False

        # Если авторизация успешна, заполняем словарь чата
        self.cur_chats[peer_id] = {
            'cur_json': cur_json,
            'session': my_session,
            'cur_domain': domain,
            'monitoring_flag': False,
            'accept_codes': True,
            'sector_monitor': True,
            'bonus_monitor': True,
            'send_screen': True,
            'parser': True,
            'route_builder': False,
            'send_code_in_block': False,
            '5_min_sent': False,
            '1_min_sent': False,
            'old_levels': {},
            'browser': {'context': None, 'page': None},
            'sector_closers': {},
            'bonus_closers': {},
            'last_coords': None}

        if self.globalconfig['USE_BROWSER'] and self.browser:
            user_agent = self.globalconfig['USER_AGENT']
            cookies_to_set = [
                {
                    'name': 'atoken',
                    'value': get_cookie('atoken', my_session),
                    'domain': domain,
                    'path': '/',
                    'secure': False,
                    'httpOnly': True
                },
                {
                    'name': 'stoken',
                    'value': get_cookie('stoken', my_session),
                    'domain': domain,
                    'path': '/',
                    'secure': False,
                    'httpOnly': False
                }
            ]
            context = await self.browser.new_context(user_agent=user_agent, storage_state={'cookies': cookies_to_set})
            my_page = await context.new_page()
            self.cur_chats[peer_id]['browser']['context'] = context
            self.cur_chats[peer_id]['browser']['page'] = my_page

        return True

    async def stop_auth(self, peer_id: str | int) -> bool:
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            await self.message_func(peer_id, 'Чат не авторизован')
            return False
        chat_data['monitoring_flag'] = False
        await chat_data['session'].close()
        await asyncio.sleep(7)
        self.cur_chats.pop(peer_id, None)  # Освобождаем в памяти словарь чата
        await self.message_func(peer_id, 'Авторизация чата отключена')
        return True

    async def get_hints(self, peer_id: str | int) -> str | None:
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            await self.message_func(peer_id, 'Чат не авторизован')
            return
        try:
            async with chat_data['session'].get(f'https://{chat_data["cur_domain"]}/GameEngines/Encounter/Play/{chat_data["cur_json"]["GameId"]}?json=1') as response:
                response.raise_for_status()
                game_json = await response.json()
        except Exception as e:
            logging.error(f"Ошибка, возможно необходимо заново авторизоваться: {e}", exc_info=True)
            await self.message_func(peer_id, f'Ошибка, возможно необходимо заново авторизоваться: {e}')
            return

        if game_json['Event'] != 0:
            await self.message_func(peer_id, f'{EN_EVENT_ERRORS.get(game_json['Event'])}')
            return

        result_str = ''
        for elem in game_json['Level']['Helps']:
            if elem['RemainSeconds'] == 0:
                result_str += f'Подсказка {elem["Number"]}:\n{elem["HelpText"]}\n{"_" * 30}\n\n'
            else:
                result_str += f'Подсказка {elem["Number"]}: Будет через {datetime.timedelta(seconds=elem["RemainSeconds"])}\n{"_" * 30}\n\n'
        if result_str == '':
            result_str = 'Нет подсказок'
        return parse_html(result_str, chat_data.get('parser', False))

    async def get_task(self, peer_id: str | int) -> str | None:
        await self.check_engine(peer_id)
        gameinfo_str, gamelevel_str = await self.get_curlevel_info(peer_id)
        return gamelevel_str

    async def get_time(self, peer_id: str | int) -> str | None:
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            await self.message_func(peer_id, 'Чат не авторизован')
            return
        try:
            async with chat_data['session'].get(f'https://{chat_data["cur_domain"]}/GameEngines/Encounter/Play/{chat_data["cur_json"]["GameId"]}?json=1') as response:
                response.raise_for_status()
                game_json = await response.json()
        except Exception as e:
            logging.error(f"Ошибка, возможно необходимо заново авторизоваться: {e}", exc_info=True)
            await self.message_func(peer_id, f'Ошибка, возможно необходимо заново авторизоваться {e}')
            return

        if game_json['Event'] != 0:
            await self.message_func(peer_id, f'{EN_EVENT_ERRORS.get(game_json['Event'])}')
            return
        if game_json["Level"]["Timeout"] == 0:
            await self.message_func(peer_id, 'Автопереход отсутствует')
            return
        return f'Автопереход через {datetime.timedelta(seconds=game_json["Level"]["TimeoutSecondsRemain"])}'

    async def get_sectors_and_bonuses(self, peer_id: str | int, sector: bool = True, levelnum: str = '0', only_left: bool = False) -> str | None:
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            await self.message_func(peer_id, 'Чат не авторизован')
            return
        if levelnum != '0':
            if levelnum in chat_data['old_levels']:
                game_json = chat_data['old_levels'][levelnum]
            else:
                await self.message_func(peer_id, 'Уровень не найден в прошедших')
                return
        else:
            try:
                async with chat_data['session'].get(f'https://{chat_data["cur_domain"]}/GameEngines/Encounter/Play/{chat_data["cur_json"]["GameId"]}?json=1') as response:
                    response.raise_for_status()
                    game_json = await response.json()
            except Exception as e:
                logging.error(f"Ошибка, возможно необходимо заново авторизоваться: {e}", exc_info=True)
                await self.message_func(peer_id, f'Ошибка, возможно необходимо заново авторизоваться: {e}')
                return

        result_str = ''
        if game_json['Event'] != 0:
            await self.message_func(peer_id, f'{EN_EVENT_ERRORS.get(game_json['Event'])}')
            return

        if sector:
            for elem in game_json['Level']['Sectors']:
                if elem['IsAnswered']:
                    if not only_left:
                        result_str += f'✅№{elem["Order"]} {elem["Name"]} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]}) {chat_data["sector_closers"].get(elem["SectorId"], "")}\n'
                else:
                    result_str += f'❌№{elem["Order"]} {elem["Name"]}\n'
            if result_str == '':
                result_str = 'Нет секторов'
            result_str = f'Осталось закрыть: {game_json["Level"]["SectorsLeftToClose"] if game_json["Level"]["SectorsLeftToClose"] > 0 else 1} из {len(game_json["Level"]["Sectors"]) if len(game_json["Level"]["Sectors"]) > 0 else 1}\n' + result_str
        else:
            for elem in game_json['Level']['Bonuses']:
                if elem['IsAnswered']:
                    result_str += f'{"🔴" if elem["Negative"] else "🟢"}№{elem["Number"]} {elem["Name"] or ""} [{elem["Help"] or ""}] {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]}) {chat_data["bonus_closers"].get(elem["BonusId"], "")} {"Штраф: " if elem["Negative"] else "Бонус: "} {datetime.timedelta(seconds=elem["AwardTime"])}\n'
                else:
                    result_str += f'{"✖Истёк" if elem["Expired"] else "❌"}№{elem["Number"]} {elem["Name"] or ""} {elem["Task"] or ""} {"Будет доступен через " + str(datetime.timedelta(seconds=elem["SecondsToStart"])) if elem["SecondsToStart"] != 0 else ""} {"Осталось на выполнение: " + str(datetime.timedelta(seconds=elem["SecondsLeft"])) if elem["SecondsLeft"] != 0 else ""}\n'
            if result_str == '':
                result_str = 'Нет бонусов'
        return parse_html(result_str, chat_data.get('parser', False))

    async def open_browser(self, peer_id: str | int) -> bool:
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            await self.message_func(peer_id, 'Чат не авторизован')
            return False
        if self.globalconfig['USE_BROWSER']:
            user_agent = self.globalconfig['USER_AGENT']
            cookies_to_set = [
                {
                    'name': 'atoken',
                    'value': get_cookie('atoken', chat_data['session']),
                    'domain': chat_data['cur_domain'],
                    'path': '/',
                    'secure': False,
                    'httpOnly': True
                },
                {
                    'name': 'stoken',
                    'value': get_cookie('stoken', chat_data['session']),
                    'domain': chat_data['cur_domain'],
                    'path': '/',
                    'secure': False,
                    'httpOnly': False
                }
            ]
            p = await async_playwright().start()
            if self.globalconfig['BROWSER_TYPE'] == 'firefox':
                browser = await p.firefox.launch(headless=False)
            elif self.globalconfig['BROWSER_TYPE'] == 'chromium':
                browser = await p.chromium.launch(headless=False)
            else:
                browser = await p.firefox.launch(headless=False)  # по умолчанию firefox
            logging.info('Виртуальный браузер запущен')
            context = await browser.new_context(user_agent=user_agent, storage_state={'cookies': cookies_to_set})
            my_page = await context.new_page()
            await my_page.goto(f'https://{chat_data["cur_domain"]}/GameEngines/Encounter/Play/{chat_data["cur_json"]["GameId"]}')
            await self.message_func(peer_id, 'Браузер запущен')
            return True
        else:
            await self.message_func(peer_id, 'Браузер отключен в конфиге')
            return False

    async def load_old_json(self, peer_id: str | int) -> bool:
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            await self.message_func(peer_id, 'Чат не авторизован')
            return False
        json_filename = str(peer_id) + '.' + str(chat_data["cur_json"]["GameId"])
        if os.path.isfile('level_snapshots/' + json_filename):
            with open('level_snapshots/' + json_filename, 'r') as json_file:
                chat_data['old_levels'].update(json.load(json_file))
            await self.message_func(peer_id, 'JSON загружен')
            return True
        else:
            await self.message_func(peer_id, 'Файл не существует')
            return False

    async def switch_flag(self, peer_id: str | int, flag_name: str, flag_state: bool) -> bool:
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            await self.message_func(peer_id, 'Чат не авторизован')
            return False
        d = {'accept_codes': 'Прием кодов',
             'sector_monitor': 'Мониторинг секторов',
             'bonus_monitor': 'Мониторинг бонусов',
             'send_screen': 'Отправка скринов',
             'parser': 'Парсер HTML',
             'send_code_in_block': 'Отправка кодов в сектор при блоке',
             'route_builder': 'Построитель маршрутов',
             }
        if flag_name not in chat_data:
            return False
        chat_data[flag_name] = flag_state
        await self.message_func(peer_id, f'{d.get(flag_name)} {"включен(а)" if flag_state else "выключен(а)"}')
        return True

    # список игроков для тегания например при АПе уровня
    async def set_players(self, peer_id: str | int, players_list: list[str]) -> bool:
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            await self.message_func(peer_id, 'Чат не авторизован')
            return False
        chat_data['players'] = players_list
        await self.message_func(peer_id, 'Список игроков установлен')
        return True

    async def set_doc(self, peer_id: str | int, url: str | None) -> bool:
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            await self.message_func(peer_id, 'Чат не авторизован')
            return False
        if url:
            chat_data['doc'] = url
            await self.message_func(peer_id, 'Ссылка на док установлена')
            return True
        else:
            chat_data['doc'] = ''
            await self.message_func(peer_id, 'Ссылка на док удалена')
            return True

    async def get_game_info(self, peer_id: str | int) -> str | None:
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            await self.message_func(peer_id, 'Чат не авторизован')
            return
        game_link = f'https://{chat_data.get("cur_domain", "")}/GameDetails.aspx?gid={chat_data["cur_json"]["GameId"]}'
        game_doc = chat_data.get('doc', 'Не установлен')
        return f'Ссылка на игру: {game_link} \nСсылка на док: {game_doc} \n'

    async def send_answer(self, peer_id: str | int, from_id: str | int, answer: str, send_to_sector: bool = False) -> str | None:
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            await self.message_func(peer_id, 'Чат не авторизован')
            return
        if not chat_data['accept_codes']:
            await self.message_func(peer_id, 'Прием кодов выключен! Для включения выполните /accept_codes')
            return

        sectors_list = []
        bonus_list = []
        block_on_level = chat_data['cur_json']['Level']['HasAnswerBlockRule']
        send_to_block = send_to_sector or chat_data['send_code_in_block']
        result_str = ''

        if (len(chat_data['cur_json']['Level']['Bonuses']) == 0) and block_on_level and not send_to_block:
            await self.message_func(peer_id, 'На уровне блокировка, в сектор вбивайте самостоятельно или через /!')
            return

        if block_on_level and not send_to_block:
            answer_type = 'BonusAction'
            await self.message_func(peer_id, 'На уровне блокировка, вбиваю в бонус, в сектор вбивайте самостоятельно или через /!')
        else:
            answer_type = 'LevelAction'

        try:
            async with chat_data["session"].get(f'https://{chat_data["cur_domain"]}/GameEngines/Encounter/Play/{chat_data["cur_json"]["GameId"]}?json=1') as response:
                response.raise_for_status()
                old_json = await response.json()
            answer_data = {'LevelId': chat_data["cur_json"]['Level']['LevelId'], 'LevelNumber': chat_data["cur_json"]['Level']['Number'], answer_type + '.answer': answer}

            async with chat_data['session'].post(f'https://{chat_data["cur_domain"]}/GameEngines/Encounter/Play/{chat_data["cur_json"]["GameId"]}?json=1', data=answer_data) as response:
                response.raise_for_status()
                answer_json = await response.json()
        except Exception as e:
            logging.error(f"Ошибка работы бота, возможно необходимо заново авторизоваться: {e}", exc_info=True)
            await self.message_func(peer_id, f'Ошибка, возможно необходимо заново авторизоваться: {e}')
            return

        if answer_json['Event'] != 0:
            await self.check_engine(peer_id)
            await self.message_func(peer_id, f'{EN_EVENT_ERRORS.get(answer_json['Event'])}')
            return

        if answer_json['EngineAction'][answer_type]['IsCorrectAnswer']:
            if answer_type == 'LevelAction':
                for elem in answer_json['Level']['Sectors']:
                    if elem['IsAnswered'] and elem["Answer"]["Answer"].lower() == answer.lower():
                        if elem in old_json['Level']['Sectors']:
                            sectors_list.append(f'⚪Баян! Сектор №{elem["Order"]} {elem["Name"] or ""}')
                        else:
                            sectors_list.append(f'🟢Сектор №{elem["Order"]} {elem["Name"] or ""} закрыт!')
                            chat_data['sector_closers'][elem["SectorId"]] = from_id

            for elem in answer_json['Level']['Bonuses']:
                if elem['IsAnswered'] and elem["Answer"]["Answer"].lower() == answer.lower():
                    if elem in old_json['Level']['Bonuses']:
                        bonus_list.append(
                            f'⚪Баян! Бонус №{elem["Number"]} {elem["Name"] or ""}\n{("Штрафное время: " if elem["Negative"] else "Бонусное время: ") + str(datetime.timedelta(seconds=elem["AwardTime"])) if elem["AwardTime"] != 0 else ""}\n{"Подсказка бонуса:" + chr(10) + elem["Help"] if elem["Help"] else ""}')
                    else:
                        bonus_list.append(
                            f'Бонус №{elem["Number"]} {elem["Name"] or ""} закрыт\n{("🔴 Штрафное время: " if elem["Negative"] else "🟢 Бонусное время: ") + str(datetime.timedelta(seconds=elem["AwardTime"])) if elem["AwardTime"] != 0 else ""}\n{"Подсказка бонуса:" + chr(10) + elem["Help"] if elem["Help"] else ""}')
                        chat_data['bonus_closers'][elem["BonusId"]] = from_id
            result_str += f'✅Ответ {answer} верный\n' + '\n'.join(sectors_list) + '\n' + '\n'.join(bonus_list)

        elif answer_json['EngineAction'][answer_type]['IsCorrectAnswer'] is False:
            result_str += f'❌Ответ {answer} неверный'

        elif answer_json['EngineAction'][answer_type]['IsCorrectAnswer'] is None:
            result_str += f'❓Ответа на код {answer} не было, возможно поле заблокировано'

        await self.check_engine(peer_id)
        return result_str

    async def check_engine(self, peer_id: str | int) -> bool:  # False - если цикл мониторинга надо прервать (Серьезная ошибка), True - если продолжать
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            return False
        try:
            async with chat_data["session"].get(f'https://{chat_data["cur_domain"]}/GameEngines/Encounter/Play/{chat_data["cur_json"]["GameId"]}?json=1&lang={self.globalconfig['LANG']}') as response:
                response.raise_for_status()
                game_json = await response.json()
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as CE:
            logging.error(f'Ошибка соединения {CE}, переподключаюсь')
            return True

        except Exception as e:
            if 'session is closed' in str(e).lower():
                await self.message_func(peer_id, 'Сессия авторизации закрыта')
            else:
                await self.message_func(peer_id, f'Ошибка мониторинга, возможно необходимо заново авторизоваться: {e}')
                logging.error(f"Ошибка мониторинга бота: {e}", exc_info=True)
            return False

        match game_json['Event']:
            case 2 | 4 | 7 | 8 | 9 | 10 | 11 | 12 | 13:
                logging.info(EN_EVENT_ERRORS.get(game_json['Event']))
                await self.message_func(peer_id, EN_EVENT_ERRORS.get(game_json['Event']))
                return False
            case 5:
                logging.info(EN_EVENT_ERRORS.get(game_json['Event']))
                return True
            case 6 | 17:
                await self.message_func(peer_id, EN_EVENT_ERRORS.get(game_json['Event']))
                chat_data['monitoring_flag'] = False
                await asyncio.sleep(7)
                await self.message_func(peer_id, 'Авторизация чата отключена')
                self.cur_chats.pop(peer_id, None)  # Освобождаем в памяти словарь чата
                return False
            case 16 | 18 | 19 | 20 | 21 | 22:
                await self.message_func(peer_id, EN_EVENT_ERRORS.get(game_json['Event']))
                return True
            case 0:
                old_json = chat_data['cur_json']  # предыдущий json
                chat_data['cur_json'] = game_json  # текущий json

                # Игра началась
                if old_json['Level'] is None:
                    await self.message_func(peer_id, 'Игра началась!\n')
                    curlevel_info = await self.get_curlevel_info(peer_id)
                    await self.message_func(peer_id, curlevel_info[0])
                    await self.message_func(peer_id, curlevel_info[1])
                    return True

                # Проверка, что поменялся номер уровня, т.е. произошел АП
                if old_json['Level']['Number'] != game_json['Level']['Number']:
                    chat_data['5_min_sent'] = False
                    chat_data['1_min_sent'] = False
                    await self.message_func(peer_id, 'АП!\n' + ' '.join(chat_data.get('players', '')))

                    if chat_data['send_screen']:
                        await self.message_func(peer_id, await self.get_screen_as_bytes_async(peer_id, full=True))

                    # отключение ввода кодов при обнаружении штрафных
                    if len(game_json['Level']['Tasks']) > 0:
                        if any(item in game_json['Level']['Tasks'][0]['TaskText'].lower() for item in self.globalconfig['STOP_ACCEPT_CODES_WORDS']):
                            chat_data['accept_codes'] = False
                            await self.message_func(peer_id, 'В тексте обнаружена информация о штрафах или ложных кодах, ввод кодов отключен! Для включения выполните /accept_codes')

                    curlevel_info = await self.get_curlevel_info(peer_id)
                    await self.message_func(peer_id, curlevel_info[0])
                    await self.message_func(peer_id, curlevel_info[1])

                    if len(game_json['Level']['Tasks']) > 0:
                        await self.send_kml_info(peer_id, game_json['Level']['Tasks'][0]['TaskText'], game_json['Level']['Number'])

                    # Сохраняем информацию о пройденном уровне
                    chat_data['old_levels'][str(old_json['Level']['Number'])] = {}
                    chat_data['old_levels'][str(old_json['Level']['Number'])]['Event'] = old_json['Event']
                    chat_data['old_levels'][str(old_json['Level']['Number'])]['Level'] = old_json['Level']

                    # Запись в файл
                    json_file_data = chat_data['old_levels']
                    json_filename = f'{peer_id}.{chat_data["cur_json"]["GameId"]}'
                    if os.path.isfile('level_snapshots/' + json_filename):
                        with open('level_snapshots/' + json_filename) as json_file:
                            json_file_data.update(json.load(json_file))
                    with open('level_snapshots/' + json_filename, 'w') as json_file:
                        json.dump(json_file_data, json_file)

                    return True

                # проверка на изменение текста уровня
                if old_json['Level']['Tasks'] != game_json['Level']['Tasks']:
                    await self.message_func(peer_id, 'Задание уровня изменилось)')

                # проверка на сообщения на уровне:
                for elem in game_json['Level']['Messages']:
                    if elem not in old_json['Level']['Messages']:
                        await self.message_func(peer_id, f'Добавлено сообщение: {elem["MessageText"]}')

                # проверка на количество секторов на уровне:
                if len(old_json['Level']['Sectors']) != len(game_json['Level']['Sectors']):
                    await self.message_func(peer_id, 'Количество секторов на уровне изменилось')

                # проверка на количество бонусов на уровне:
                if len(old_json['Level']['Bonuses']) != len(game_json['Level']['Bonuses']):
                    await self.message_func(peer_id, 'Количество бонусов на уровне изменилось')

                # проверка на количество необходимых секторов:
                if old_json['Level']['RequiredSectorsCount'] != game_json['Level']['RequiredSectorsCount']:
                    await self.message_func(peer_id, 'Количество необходимых для прохождения секторов изменилось')

                # проверка на кол-во оставшихся секторов:
                cur_sectors_left = game_json['Level']['SectorsLeftToClose']
                if old_json['Level']['SectorsLeftToClose'] != cur_sectors_left and cur_sectors_left <= self.globalconfig['SECTORS_LEFT_ALERT']:
                    sector_list = [str(elem['Name']) for elem in game_json['Level']['Sectors'] if not (elem['IsAnswered'])]
                    await self.message_func(peer_id, f'Осталось секторов: [{cur_sectors_left}]. Оставшиеся: {", ".join(sector_list)}')

                # Проверка, что пришла подсказка
                if len(chat_data["cur_json"]['Level']['Helps']) != len(old_json['Level']['Helps']):
                    await self.message_func(peer_id, 'Была добавлена подсказка')
                else:
                    for i, elem in enumerate(chat_data["cur_json"]['Level']['Helps']):
                        if elem['HelpText'] != old_json['Level']['Helps'][i]['HelpText']:
                            await self.message_func(peer_id, f'Подсказка {i + 1}: {parse_html(elem["HelpText"], chat_data.get('parser', False))}')
                            await self.send_kml_info(peer_id, elem["HelpText"], game_json['Level']['Number'])

                # мониторинг закрытия секторов
                if chat_data['sector_monitor']:
                    sector_msg = ''
                    for elem in game_json['Level']['Sectors']:
                        if elem not in old_json['Level']['Sectors'] and elem["IsAnswered"] and (elem['SectorId'] not in chat_data['sector_closers']):
                            sector_msg += f'✅№{elem["Order"]} {elem["Name"]} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]})\n'
                    if sector_msg != '':
                        await self.message_func(peer_id, sector_msg)

                # мониторинг закрытия бонусов
                if chat_data['bonus_monitor']:
                    for elem in game_json['Level']['Bonuses']:
                        if elem not in old_json['Level']['Bonuses'] and elem["IsAnswered"] and (elem['BonusId'] not in chat_data['sector_closers']):
                            if elem.get('Help'):
                                bonus_hint = f"Подсказка бонуса:\n{parse_html(elem.get("Help", ''), chat_data.get('parser', False))}"
                                await self.send_kml_info(peer_id, elem["Help"], chat_data["cur_json"]["Level"]["Number"])
                            else:
                                bonus_hint = ''
                            await self.message_func(peer_id, f'{"🔴" if elem["Negative"] else "🟢"} №{elem["Number"]} {elem["Name"] or ""} {elem["Answer"]["Answer"]} ({elem["Answer"]["Login"]}) {"Штраф: " if elem["Negative"] else "Бонус: "} {datetime.timedelta(seconds=elem["AwardTime"])}\n{bonus_hint}')

                # мониторинг времени до автоперехода
                if self.globalconfig['TIMELEFT_ALERT1'] > game_json['Level']['TimeoutSecondsRemain'] > 0 and not (chat_data['5_min_sent']):
                    await self.message_func(peer_id, 'До автоперехода осталось менее 5 минут!')

                    chat_data['5_min_sent'] = True
                if self.globalconfig['TIMELEFT_ALERT2'] > game_json['Level']['TimeoutSecondsRemain'] > 0 and not (chat_data['1_min_sent']):
                    await self.message_func(peer_id, 'До автоперехода осталось менее минуты!')
                    chat_data['1_min_sent'] = True
        return True

    async def monitoring_func(self, peer_id: str | int) -> None:
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            await self.message_func(peer_id, 'Чат не авторизован')
            return
        start_time = datetime.datetime.now()
        await self.message_func(peer_id, 'Мониторинг включен')
        while chat_data['monitoring_flag']:
            print(f'Слежение за игрой в чате {peer_id} работает {datetime.datetime.now() - start_time}')
            await asyncio.sleep(self.globalconfig['CHECK_INTERVAL'] + random.uniform(-1, 1))
            try:
                if not (await self.check_engine(peer_id)):
                    break
            except Exception as e:
                logging.error(f"Ошибка функции check_engine, продолжаю мониторинг: {e}", exc_info=True)
        chat_data['monitoring_flag'] = False
        await self.message_func(peer_id, 'Мониторинг выключен')

    async def game_monitor(self, peer_id: str | int, state: bool) -> bool:
        chat_data = self.cur_chats.get(peer_id)
        if not chat_data:
            await self.message_func(peer_id, 'Чат не авторизован')
            return False
        if not state:
            chat_data['monitoring_flag'] = False
        else:
            if not chat_data['monitoring_flag']:
                chat_data['monitoring_flag'] = True
                asyncio.create_task(self.monitoring_func(peer_id))
                return True
            else:
                await self.message_func(peer_id, 'Слежение уже запущено')
                return True
