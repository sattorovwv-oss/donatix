"""FSM-состояния диалогов."""
from aiogram.fsm.state import State, StatesGroup


class Buy(StatesGroup):
    quantity = State()          # ждём количество звёзд
    recipient = State()         # ждём @username получателя
    check_recipient = State()   # показали имя аккаунта, ждём «да, это он»
    confirm = State()           # показали сводку, ждём подтверждения
    promo = State()             # ждём промокод на скидку


class Steam(StatesGroup):
    login = State()         # ждём логин Steam
    confirm = State()       # показали аккаунт, ждём подтверждения


class Game(StatesGroup):
    player = State()        # ждём ID игрока
    confirm = State()       # показали ник, ждём подтверждения


class GameNew(StatesGroup):
    data = State()          # ждём «код_игры Название» при добавлении



class Deposit(StatesGroup):
    amount = State()      # ждём сумму пополнения
    receipt = State()     # ждём скриншот чека
    # Оплата из России: там сумму не назначаем мы, её называет клиент —
    # ровно ту, что банк написал ему в чеке, вместе с копейками.
    from_receipt = State()


class Promo(StatesGroup):
    code = State()


class Calc(StatesGroup):
    query = State()


class Support(StatesGroup):
    subject = State()     # первое сообщение тикета
    reply = State()       # дописка в открытый тикет


class Panel(StatesGroup):
    value = State()       # ждём новое значение настройки
    emoji = State()       # ждём новый значок
    period = State()      # ждём даты для отчёта
    user_search = State() # ждём ID или юзернейм клиента
    adjust = State()      # ждём сумму правки баланса
    transfer = State()    # ждём список балансов со старого бота
    link = State()        # ждём название рекламной ссылки
    admin_add = State()   # ждём ID того, кому открыть панель
    game_find = State()   # ждём название игры для поиска по каталогу
    ru_photo = State()    # ждём картинку-пример чека из Сбербанка


class Review(StatesGroup):
    text = State()        # ждём текст отзыва после оценки


class PartnerNew(StatesGroup):
    name = State()        # ждём имя партнёра
    share = State()       # ждём его долю в процентах


class PartnerMove(StatesGroup):
    amount = State()      # ждём сумму взноса или выплаты


class Cast(StatesGroup):
    content = State()     # ждём сообщение для рассылки
    buttons = State()     # ждём список кнопок
    confirm = State()     # готово к отправке


class PromoNew(StatesGroup):
    data = State()        # ждём «КОД сумма лимит» (старый однострочный ввод)
    # Пошаговое создание промокода на скидку
    code = State()        # ждём сам код
    percent = State()     # ждём процент скидки
    limit = State()       # ждём число активаций
    confirm = State()     # показали сводку, ждём «Сохранить»


class ApiCab(StatesGroup):
    """Кабинет разработчика: всё, что бот у него спрашивает."""
    key_label = State()     # ждём название нового ключа
    hook_url = State()      # ждём адрес вебхука
