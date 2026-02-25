"""
quizbattle - командная викторина в реальном времени
полностью переписанная версия с бд, авторизацией и всеми фичами
"""

import os
import random
import string
import json
import time
import threading
import uuid
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, send_file
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from sqlalchemy import func
import requests
from dotenv import load_dotenv

# загружаем переменные окружения из .env
load_dotenv()

# инициализация flask приложения
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///quizbattle.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# инициализация расширений flask
db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ==================== МОДЕЛИ БД ====================

class User(UserMixin, db.Model):
    """пользователь системы"""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    avatar = db.Column(db.String(200), default='default.png')
    
    # статистика
    total_games = db.Column(db.Integer, default=0)
    total_wins = db.Column(db.Integer, default=0)
    total_points = db.Column(db.Integer, default=0)
    rating = db.Column(db.Integer, default=1000)  # elo-like рейтинг
    
    def __repr__(self):
        return f'<User {self.username}>'


class Question(db.Model):
    """вопрос для викторины"""
    id = db.Column(db.Integer, primary_key=True)
    topic = db.Column(db.String(50), nullable=False)
    difficulty = db.Column(db.String(20), default='medium')  # easy, medium, hard
    question_text = db.Column(db.Text, nullable=False)
    option_1 = db.Column(db.String(200), nullable=False)
    option_2 = db.Column(db.String(200), nullable=False)
    option_3 = db.Column(db.String(200), nullable=False)
    option_4 = db.Column(db.String(200), nullable=False)
    correct_answer = db.Column(db.Integer, nullable=False)  # 0-3
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    times_used = db.Column(db.Integer, default=0)
    
    def to_dict(self):
        return {
            'id': self.id,
            'question': self.question_text,
            'options': [self.option_1, self.option_2, self.option_3, self.option_4],
            'correct': self.correct_answer,
            'difficulty': self.difficulty
        }


class GameHistory(db.Model):
    """история игр"""
    id = db.Column(db.Integer, primary_key=True)
    pin = db.Column(db.String(6), nullable=False)
    topic = db.Column(db.String(50), nullable=False)
    mode = db.Column(db.String(20), default='teams')  # teams, ffa
    difficulty = db.Column(db.String(20), default='medium')
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    ended_at = db.Column(db.DateTime)
    winner_team = db.Column(db.String(10))  # A, B, или user_id для ffa
    questions_count = db.Column(db.Integer, default=10)
    
    # связи
    creator = db.relationship('User', backref='created_games')
    players = db.relationship('PlayerStats', backref='game', lazy=True)


class PlayerStats(db.Model):
    """статистика игрока в конкретной игре"""
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('game_history.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    guest_name = db.Column(db.String(50), nullable=True)  # для гостей
    team = db.Column(db.String(10))  # A, B или null для ffa
    score = db.Column(db.Integer, default=0)
    correct_answers = db.Column(db.Integer, default=0)
    wrong_answers = db.Column(db.Integer, default=0)
    avg_response_time = db.Column(db.Float, default=0)
    
    user = db.relationship('User', backref='game_stats')


# ==================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ====================

# активные игры в памяти (для real-time)
active_games = {}
games_lock = threading.Lock()

# темы для выбора (без эмодзи)
TOPICS = [
    'история',
    'наука',
    'география', 
    'спорт',
    'кино и музыка',
    'технологии',
    'литература',
    'биология',
    'космос',
    'видеоигры'
]

# конфигурация для API Кими
KIMI_API_KEY = os.environ.get('KIMI_API_KEY', '')
KIMI_API_URL = os.environ.get('KIMI_API_URL', 'https://api.moonshot.cn/v1/chat/completions')


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def generate_pin():
    """генерация уникального 6-значного пин-кода"""
    while True:
        pin = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        with games_lock:
            if pin not in active_games:
                return pin


def generate_questions_via_kimi(topic, difficulty, count=35):
    """генерация вопросов через API Кими"""
    if not KIMI_API_KEY:
        print("[!] нет API ключа")
        return None
    
    # составляем промпт для API
    prompt = f"""Создай {count} вопросов для викторины на тему "{topic}" с уровнем сложности "{difficulty}".

Требования:
- Вопросы должны быть разнообразными и интересными
- 4 варианта ответа на каждый вопрос
- Только один правильный ответ

Ответь СТРОГО в формате JSON:
{{
  "questions": [
    {{
      "question": "текст вопроса",
      "options": ["вариант 1", "вариант 2", "вариант 3", "вариант 4"],
      "correct": 0
    }}
  ]
}}

gде correct - индекс правильного ответа (0-3)."""

    try:
        headers = {
            'Authorization': f'Bearer {KIMI_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        data = {
            'model': 'moonshot-v1-8k',
            'messages': [
                {'role': 'system', 'content': 'ты - генератор вопросов для викторины. отвечай только в формате json.'},
                {'role': 'user', 'content': prompt}
            ],
            'temperature': 0.7
        }
        
        response = requests.post(KIMI_API_URL, headers=headers, json=data, timeout=60)
        
        if response.status_code == 200:
            result = response.json()
            content = result['choices'][0]['message']['content']
            
            # парсим json из ответа
            try:
                # ищем json в тексте
                start = content.find('{')
                end = content.rfind('}') + 1
                if start != -1 and end != 0:
                    json_str = content[start:end]
                    parsed = json.loads(json_str)
                    return parsed.get('questions', [])
            except json.JSONDecodeError:
                pass
                
    except Exception as e:
        print(f"ошибка генерации через kimi: {e}")
    
    return None


def save_questions_to_db(topic, questions, difficulty='medium'):
    """сохранение сгенерированных вопросов в бд"""
    for q in questions:
        try:
            question = Question(
                topic=topic,
                difficulty=difficulty,
                question_text=q['question'],
                option_1=q['options'][0],
                option_2=q['options'][1],
                option_3=q['options'][2],
                option_4=q['options'][3],
                correct_answer=q['correct']
            )
            db.session.add(question)
        except Exception as e:
            print(f"ошибка сохранения вопроса: {e}")
            continue
    
    db.session.commit()


def get_random_questions(topic, count=10, difficulty=None):
    """получение случайных вопросов из бд"""
    query = Question.query.filter_by(topic=topic)
    
    if difficulty and difficulty != 'mixed':
        query = query.filter_by(difficulty=difficulty)
    
    questions = query.order_by(func.random()).limit(count).all()
    return [q.to_dict() for q in questions]


# ==================== КЛАСС ИГРЫ ====================

class GameSession:
    """класс управления игровой сессией"""
    
    def __init__(self, creator_id, topic, mode='teams', difficulty='medium', questions_count=10, has_password=False, password=None):
        self.pin = generate_pin()
        self.creator_id = creator_id
        self.topic = topic
        self.mode = mode
        self.difficulty = difficulty
        self.questions_count = questions_count
        self.has_password = has_password
        self.password = password
        
        self.status = 'waiting'
        self.created_at = time.time()
        
        # игроки
        self.players = {}  # sid -> {user_id, name, team, score, correct, wrong, times}
        self.teams = {'A': [], 'B': []}
        
        # вопросы
        self.questions = []
        self.current_question_idx = 0
        
        # таймер и состояние
        self.question_start_time = None
        self.answered_this_round = set()
        self.current_team = 'A'  # для режима команд
        
        # бонусы
        self.bonus_enabled = True
        
    def add_player(self, sid, user_id=None, guest_name=None):
        """добавление игрока в игру"""
        if self.mode == 'teams':
            # балансировка команд
            team_a = len(self.teams['A'])
            team_b = len(self.teams['B'])
            team = 'A' if team_a <= team_b else 'B'
            self.teams[team].append(sid)
        else:
            team = None
            
        self.players[sid] = {
            'user_id': user_id,
            'name': guest_name or (User.query.get(user_id).username if user_id else 'игрок'),
            'team': team,
            'score': 0,
            'correct': 0,
            'wrong': 0,
            'response_times': [],
            'answered_current': False
        }
        
        return team
    
    def remove_player(self, sid):
        """удаление игрока"""
        if sid in self.players:
            team = self.players[sid]['team']
            if team and sid in self.teams[team]:
                self.teams[team].remove(sid)
            del self.players[sid]
    
    def load_questions(self):
        """загрузка вопросов из бд или генерация через API"""
        questions = get_random_questions(self.topic, self.questions_count, self.difficulty)
        
        # если не хватает вопросов - генерируем новые
        if len(questions) < self.questions_count and KIMI_API_KEY:
            new_questions = generate_questions_via_kimi(self.topic, self.difficulty, 20)
            if new_questions:
                save_questions_to_db(self.topic, new_questions, self.difficulty)
                questions = get_random_questions(self.topic, self.questions_count, self.difficulty)
        
        self.questions = questions[:self.questions_count]
    
    def get_current_question(self):
        """получение текущего вопроса"""
        if 0 <= self.current_question_idx < len(self.questions):
            q = self.questions[self.current_question_idx]
            return {
                'question': q['question'],
                'options': q['options'],
                'question_number': self.current_question_idx + 1,
                'total': len(self.questions),
                'current_team': self.current_team if self.mode == 'teams' else None
            }
        return None
    
    def check_answer(self, answer_idx):
        """проверка ответа"""
        if 0 <= self.current_question_idx < len(self.questions):
            return answer_idx == self.questions[self.current_question_idx]['correct']
        return False
    
    def calculate_score(self, is_correct, response_time):
        """расчет очков с бонусом за скорость"""
        if not is_correct:
            return 0
        
        base_score = 10
        
        # бонус за скорость ответа
        if self.bonus_enabled and response_time < 10:
            speed_bonus = int((10 - response_time) * 2)
            base_score += speed_bonus
        
        # модификатор сложности
        if self.difficulty == 'hard':
            base_score += 5
        elif self.difficulty == 'easy':
            base_score -= 2
            
        return max(1, base_score)
    
    def next_question(self):
        """переход к следующему вопросу"""
        self.current_question_idx += 1
        self.answered_this_round.clear()
        
        # сбрасываем флаги ответов
        for p in self.players.values():
            p['answered_current'] = False
        
        # меняем команду
        if self.mode == 'teams':
            self.current_team = 'B' if self.current_team == 'A' else 'A'
        
        return self.current_question_idx < len(self.questions)
    
    def get_leaderboard(self):
        """получение таблицы лидеров"""
        if self.mode == 'teams':
            score_a = sum(p['score'] for p in self.players.values() if p['team'] == 'A')
            score_b = sum(p['score'] for p in self.players.values() if p['team'] == 'B')
            return {'A': score_a, 'B': score_b}
        else:
            # ffa режим
            sorted_players = sorted(
                self.players.items(),
                key=lambda x: x[1]['score'],
                reverse=True
            )
            return [{'sid': sid, **data} for sid, data in sorted_players]
    
    def get_stats(self):
        """детальная статистика для админа"""
        stats = []
        for sid, p in self.players.items():
            avg_time = sum(p['response_times']) / len(p['response_times']) if p['response_times'] else 0
            stats.append({
                'name': p['name'],
                'team': p['team'],
                'score': p['score'],
                'correct': p['correct'],
                'wrong': p['wrong'],
                'avg_time': round(avg_time, 2)
            })
        return sorted(stats, key=lambda x: x['score'], reverse=True)


# ==================== РОУТЫ ====================

@app.route('/')
def index():
    """главная страница"""
    return render_template('index.html', topics=TOPICS)


@app.route('/register', methods=['GET', 'POST'])
def register():
    """регистрация нового пользователя"""
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        
        if not username or not email or not password:
            flash('заполните все поля')
            return redirect(url_for('register'))
        
        if User.query.filter_by(username=username).first():
            flash('такой username уже занят')
            return redirect(url_for('register'))
        
        if User.query.filter_by(email=email).first():
            flash('такой email уже зарегистрирован')
            return redirect(url_for('register'))
        
        user = User(
            username=username,
            email=email,
            password_hash=bcrypt.generate_password_hash(password).decode('utf-8')
        )
        db.session.add(user)
        db.session.commit()
        
        flash('регистрация успешна! теперь войдите')
        return redirect(url_for('login'))
    
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    """вход в систему"""
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        user = User.query.filter_by(username=username).first()
        
        if user and bcrypt.check_password_hash(user.password_hash, password):
            login_user(user, remember=True)
            return redirect(url_for('index'))
        
        flash('неверный логин или пароль')
    
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    """выход из системы"""
    logout_user()
    return redirect(url_for('index'))


@app.route('/profile')
@login_required
def profile():
    """профиль пользователя"""
    # статистика игр
    stats = PlayerStats.query.filter_by(user_id=current_user.id).all()
    
    # позиция в рейтинге
    rank = User.query.filter(User.rating > current_user.rating).count() + 1
    
    return render_template('profile.html', stats=stats, rank=rank)


@app.route('/rating')
def rating():
    """таблица рейтинга - показываем только тех кто хоть раз играл"""
    # показываем только тех кто хотя бы раз играл
    users = User.query.filter(User.total_games > 0).order_by(User.rating.desc()).limit(100).all()
    return render_template('rating.html', users=users)


@app.route('/join', methods=['POST'])
def join():
    """присоединение к игре по POST"""
    pin = request.form.get('pin', '').upper().strip()
    guest_name = request.form.get('guest_name', '').strip()
    password = request.form.get('password', '')
    
    if not pin or not guest_name:
        flash('заполните все поля')
        return redirect(url_for('index'))
    
    # проверяем существование игры
    with games_lock:
        if pin not in active_games:
            flash('игра не найдена')
            return redirect(url_for('index'))
        
        game = active_games[pin]
        if game.status != 'waiting':
            flash('игра уже началась')
            return redirect(url_for('index'))
        
        if game.has_password and game.password != password:
            flash('неверный пароль')
            return redirect(url_for('index'))
    
    # сохраняем в сессию для websocket
    session['game_pin'] = pin
    session['guest_name'] = guest_name
    
    return redirect(url_for('lobby', pin=pin))


@app.route('/lobby')
def lobby():
    """страница лобби"""
    pin = request.args.get('pin', '').upper()
    if not pin:
        return redirect(url_for('index'))
    return render_template('lobby.html', pin=pin)


@app.route('/game')
def game():
    """игровая страница"""
    pin = request.args.get('pin', '').upper()
    if not pin:
        return redirect(url_for('index'))
    return render_template('game.html', pin=pin)


# ==================== SOCKET.IO ====================

@socketio.on('connect')
def handle_connect():
    """подключение клиента"""
    # просто коннект, ничего интересного
    pass


@socketio.on('disconnect')
def handle_disconnect():
    """отключение клиента"""
    with games_lock:
        for pin, game in list(active_games.items()):
            if request.sid in game.players:
                game.remove_player(request.sid)
                
                # уведомляем остальных
                emit('player_left', {
                    'name': game.players.get(request.sid, {}).get('name', 'игрок'),
                    'players': get_players_list(game)
                }, room=pin)
                
                # если не осталось игроков - удаляем игру
                if len(game.players) == 0:
                    del active_games[pin]
                break


def get_players_list(game):
    """получение списка игроков для фронта"""
    result = []
    for sid, p in game.players.items():
        result.append({
            'name': p['name'],
            'team': p['team'],
            'score': p['score']
        })
    return result


@socketio.on('create_game')
def handle_create_game(data):
    """создание новой игры"""
    topic = data.get('topic')
    mode = data.get('mode', 'teams')  # teams или ffa
    difficulty = data.get('difficulty', 'medium')
    questions_count = int(data.get('questions_count', 10))
    has_password = data.get('has_password', False)
    password = data.get('password')
    
    # создаем сессию
    game = GameSession(
        creator_id=current_user.id if current_user.is_authenticated else None,
        topic=topic,
        mode=mode,
        difficulty=difficulty,
        questions_count=questions_count,
        has_password=has_password,
        password=password
    )
    
    with games_lock:
        active_games[game.pin] = game
    
    join_room(game.pin)
    
    emit('game_created', {
        'pin': game.pin,
        'topic': topic,
        'mode': mode,
        'difficulty': difficulty,
        'questions_count': questions_count
    })


@socketio.on('join_game')
def handle_join_game(data):
    """присоединение к игре"""
    pin = data.get('pin', '').upper().strip()
    guest_name = data.get('guest_name', '').strip()
    password = data.get('password')
    
    with games_lock:
        if pin not in active_games:
            emit('error', {'message': 'игра не найдена'})
            return
        
        game = active_games[pin]
        
        if game.status != 'waiting':
            emit('error', {'message': 'игра уже началась'})
            return
        
        # проверка пароля
        if game.has_password and game.password != password:
            emit('error', {'message': 'неверный пароль'})
            return
        
        # добавляем игрока
        user_id = current_user.id if current_user.is_authenticated else None
        name = guest_name or (current_user.username if current_user.is_authenticated else 'игрок')
        
        team = game.add_player(request.sid, user_id, name)
    
    join_room(pin)
    
    emit('joined', {
        'pin': pin,
        'name': name,
        'team': team,
        'mode': game.mode
    })
    
    emit('player_joined', {
        'name': name,
        'team': team,
        'players': get_players_list(game)
    }, room=pin)


@socketio.on('start_game')
def handle_start_game(data):
    """начало игры"""
    pin = data.get('pin')
    
    with games_lock:
        if pin not in active_games:
            return
        
        game = active_games[pin]
        
        # проверяем что это создатель
        if request.sid not in game.players or game.players[request.sid].get('user_id') != game.creator_id:
            emit('error', {'message': 'только создатель может начать игру'})
            return
        
        if len(game.players) < 2:
            emit('error', {'message': 'нужно минимум 2 игрока'})
            return
        
        # загружаем вопросы
        game.load_questions()
        
        if len(game.questions) == 0:
            emit('error', {'message': 'не удалось загрузить вопросы'})
            return
        
        game.status = 'playing'
    
    # сохраняем в бд
    history = GameHistory(
        pin=pin,
        topic=game.topic,
        mode=game.mode,
        difficulty=game.difficulty,
        created_by=game.creator_id,
        questions_count=game.questions_count
    )
    db.session.add(history)
    db.session.commit()
    
    emit('game_started', {
        'mode': game.mode,
        'topic': game.topic
    }, room=pin)


@socketio.on('get_question')
def handle_get_question(data):
    """получение текущего вопроса"""
    pin = data.get('pin')
    
    with games_lock:
        if pin not in active_games:
            return
        
        game = active_games[pin]
        q = game.get_current_question()
        
        if q:
            game.question_start_time = time.time()
            
            # определяем чья очередь (для команд)
            player_team = game.players.get(request.sid, {}).get('team')
            is_your_turn = (game.mode == 'ffa' or player_team == game.current_team)
            
            emit('question', {
                **q,
                'your_team': player_team,
                'is_your_turn': is_your_turn,
                'time_left': 20
            }, room=pin)
            
            # запускаем таймер
            threading.Timer(20.0, lambda: time_up(pin, game.current_question_idx)).start()
        else:
            # игра окончена
            end_game(pin)


def time_up(pin, question_idx):
    """обработка истечения времени на вопрос"""
    with games_lock:
        if pin not in active_games:
            return
        
        game = active_games[pin]
        if game.current_question_idx != question_idx:
            return
        
        # переходим к следующему
        if not game.next_question():
            end_game(pin)
            return
    
    socketio.emit('time_up', {}, room=pin)


@socketio.on('submit_answer')
def handle_submit_answer(data):
    """обработка ответа игрока"""
    pin = data.get('pin')
    answer = data.get('answer')
    
    with games_lock:
        if pin not in active_games:
            return
        
        game = active_games[pin]
        player = game.players.get(request.sid)
        
        if not player or player['answered_current']:
            return
        
        # для команд - проверяем очередь
        if game.mode == 'teams' and player['team'] != game.current_team:
            emit('error', {'message': 'сейчас очередь другой команды'})
            return
        
        # проверяем ответ
        is_correct = game.check_answer(answer)
        response_time = time.time() - game.question_start_time
        
        # начисляем очки
        points = game.calculate_score(is_correct, response_time)
        player['score'] += points
        
        if is_correct:
            player['correct'] += 1
        else:
            player['wrong'] += 1
        
        player['response_times'].append(response_time)
        player['answered_current'] = True
        game.answered_this_round.add(request.sid)
    
    # отправляем результат
    emit('answer_result', {
        'correct': is_correct,
        'points': points,
        'answer': answer
    })
    
    # обновляем счет всем
    emit('score_update', {
        'leaderboard': game.get_leaderboard(),
        'answered_by': player['name'],
        'is_correct': is_correct
    }, room=pin)
    
    # проверяем все ли ответили
    check_all_answered(pin)


def check_all_answered(pin):
    """проверка что все ответили"""
    with games_lock:
        if pin not in active_games:
            return
        
        game = active_games[pin]
        
        if game.mode == 'teams':
            # проверяем что вся команда ответила
            team_players = game.teams[game.current_team]
            answered_in_team = [p for p in team_players if game.players[p]['answered_current']]
            
            if len(answered_in_team) < len(team_players):
                return
        else:
            # ffa - ждем всех
            if len(game.answered_this_round) < len(game.players):
                return
        
        # небольшая пауза и следующий вопрос
        time.sleep(2)
        
        if not game.next_question():
            end_game(pin)
            return
    
    socketio.emit('next_question_ready', {}, room=pin)


def end_game(pin):
    """завершение игры и подсчет результатов"""
    with games_lock:
        if pin not in active_games:
            return
        
        game = active_games[pin]
        game.status = 'finished'
        
        # определяем победителя
        leaderboard = game.get_leaderboard()
        
        if game.mode == 'teams':
            winner = 'A' if leaderboard['A'] > leaderboard['B'] else 'B' if leaderboard['B'] > leaderboard['A'] else 'tie'
        else:
            # для ffa берем топ-1
            winner = leaderboard[0]['name'] if leaderboard else None
        
        # сохраняем статистику
        history = GameHistory.query.filter_by(pin=pin).first()
        if history:
            history.ended_at = datetime.utcnow()
            history.winner_team = winner if isinstance(winner, str) else None
            db.session.commit()
            
            # сохраняем статистику игроков
            for sid, p in game.players.items():
                stats = PlayerStats(
                    game_id=history.id,
                    user_id=p['user_id'],
                    guest_name=p['name'] if not p['user_id'] else None,
                    team=p['team'],
                    score=p['score'],
                    correct_answers=p['correct'],
                    wrong_answers=p['wrong'],
                    avg_response_time=sum(p['response_times']) / len(p['response_times']) if p['response_times'] else 0
                )
                db.session.add(stats)
                
                # обновляем рейтинг пользователя
                if p['user_id']:
                    user = User.query.get(p['user_id'])
                    if user:
                        user.total_games += 1
                        user.total_points += p['score']
                        
                        # обновляем рейтинг (простая elo-like система)
                        if game.mode == 'teams':
                            is_winner = (p['team'] == winner)
                        else:
                            is_winner = (p['name'] == winner)
                        
                        if is_winner:
                            user.total_wins += 1
                            user.rating += 15
                        else:
                            user.rating = max(100, user.rating - 10)
            
            db.session.commit()
        
        stats = game.get_stats()
    
    emit('game_finished', {
        'winner': winner,
        'leaderboard': leaderboard,
        'stats': stats,
        'mode': game.mode
    }, room=pin)


# админ команды
@socketio.on('admin_pause')
def handle_pause(data):
    """пауза игры"""
    pin = data.get('pin')
    
    with games_lock:
        if pin not in active_games:
            return
        
        game = active_games[pin]
        if game.players[request.sid].get('user_id') != game.creator_id:
            return
        
        game.status = 'paused'
    
    emit('game_paused', {}, room=pin)


@socketio.on('admin_skip')
def handle_skip(data):
    """пропуск текущего вопроса"""
    pin = data.get('pin')
    
    with games_lock:
        if pin not in active_games:
            return
        
        game = active_games[pin]
        if game.players[request.sid].get('user_id') != game.creator_id:
            return
        
        if not game.next_question():
            end_game(pin)
            return
    
    emit('question_skipped', {}, room=pin)


@socketio.on('admin_kick')
def handle_kick(data):
    """исключение игрока из игры"""
    pin = data.get('pin')
    target_sid = data.get('target_sid')
    
    with games_lock:
        if pin not in active_games:
            return
        
        game = active_games[pin]
        if game.players[request.sid].get('user_id') != game.creator_id:
            return
        
        if target_sid in game.players:
            name = game.players[target_sid]['name']
            game.remove_player(target_sid)
    
    emit('player_kicked', {'name': name}, room=pin)
    leave_room(pin, sid=target_sid)


# ==================== API РОУТЫ ====================

@app.route('/api/game/<pin>/stats')
def get_game_stats(pin):
    """получение статистики игры"""
    with games_lock:
        if pin in active_games:
            game = active_games[pin]
            return jsonify(game.get_stats())
    
    # если игра уже в бд
    history = GameHistory.query.filter_by(pin=pin).first()
    if history:
        stats = PlayerStats.query.filter_by(game_id=history.id).all()
        return jsonify([{
            'name': s.guest_name or (s.user.username if s.user else 'игрок'),
            'team': s.team,
            'score': s.score,
            'correct': s.correct_answers,
            'wrong': s.wrong_answers,
            'avg_time': s.avg_response_time
        } for s in stats])
    
    return jsonify({'error': 'игра не найдена'}), 404


@app.route('/api/game/<pin>/export')
def export_game_results(pin):
    """экспорт результатов в json"""
    history = GameHistory.query.filter_by(pin=pin).first()
    if not history:
        return jsonify({'error': 'игра не найдена'}), 404
    
    stats = PlayerStats.query.filter_by(game_id=history.id).all()
    
    data = {
        'pin': pin,
        'topic': history.topic,
        'mode': history.mode,
        'difficulty': history.difficulty,
        'created_at': history.created_at.isoformat(),
        'ended_at': history.ended_at.isoformat() if history.ended_at else None,
        'winner': history.winner_team,
        'players': [{
            'name': s.guest_name or (s.user.username if s.user else 'игрок'),
            'team': s.team,
            'score': s.score,
            'correct': s.correct_answers,
            'wrong': s.wrong_answers
        } for s in stats]
    }
    
    # сохраняем во временный файл
    filename = f'game_{pin}_{int(time.time())}.json'
    filepath = os.path.join('/tmp', filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    return send_file(filepath, as_attachment=True, download_name=filename)


# ==================== ИНИЦИАЛИЗАЦИЯ ====================

def init_db():
    """инициализация базы данных"""
    with app.app_context():
        db.create_all()
        print("база данных создана")


if __name__ == '__main__':
    init_db()
    print("=" * 50)
    print("quizbattle сервер запущен")
    print("=" * 50)
    print("открой http://localhost:5000 в браузере")
    print("=" * 50)
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
