import subprocess
import time
import psutil
import os
import threading
import webview
from flask import Flask, render_template_string, jsonify
import requests
import win32gui
import win32con
import tempfile
from collections import defaultdict

# ------------------- CONFIG -------------------
ANKI_CONNECT_URL = "http://localhost:8765"
ANKI_VERSION = 6
ANKI_PATH = os.path.join(os.environ["USERPROFILE"], "AppData", "Local", "Programs", "Anki", "anki.exe")

app = Flask(__name__)

deck_cache = {}
deck_list_cache = None
cache_data = {"stats": ({}, 0, 0), "timestamp": 0}

# ------------------- Helpers -------------------
def is_anki_running():
    return any(
        "anki" in proc.info['name'].lower()
        for proc in psutil.process_iter(['name'])
        if proc.info['name']
    )

def start_anki_silently():
    if is_anki_running():
        print("Anki already running")
        return

    print("Starting Anki in background...")
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    subprocess.Popen(
        [ANKI_PATH],
        startupinfo=si,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    )
    time.sleep(5)
    print("AnkiConnect ready")

def hide_anki_window(title_substring="anki", timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        found = False
        def enumHandler(hwnd, lParam):
            nonlocal found
            if title_substring.lower() in win32gui.GetWindowText(hwnd).lower():
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                found = True
        win32gui.EnumWindows(enumHandler, None)
        if found:
            return True
        time.sleep(0.5)
    return False

def wait_for_ankiconnect(timeout=15):
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.post(ANKI_CONNECT_URL, json={"action": "version", "version": ANKI_VERSION})
            if r.ok:
                print("AnkiConnect ready")
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(0.5)
    print("[!] Timeout waiting for AnkiConnect")
    return False

def invoke(action, **params):
    r = requests.post(ANKI_CONNECT_URL, json={"action": action, "version": ANKI_VERSION, "params": params})
    r.raise_for_status()
    return r.json()["result"]

def get_cached_decks():
    global deck_list_cache
    if deck_list_cache is None:
        deck_list_cache = invoke("deckNames")
    return deck_list_cache

def preload_deck(deck):
    card_ids = invoke("findCards", query=f'deck:"{deck}" is:due')
    if not card_ids:
        return []
    cards_info = invoke("cardsInfo", cards=card_ids)
    note_ids = [c["note"] for c in cards_info]
    notes_info = invoke("notesInfo", notes=note_ids)
    note_map = {n["noteId"]: n for n in notes_info}
    return [
        {
            "card_id": c["cardId"],
            "question": list(note_map[c["note"]]["fields"].values())[0]["value"],
            "answer": list(note_map[c["note"]]["fields"].values())[-1]["value"]
        } for c in cards_info
    ]

def get_cached_deck(deck):
    if deck not in deck_cache:
        deck_cache[deck] = preload_deck(deck)
    return deck_cache[deck]

def build_deck_tree(decks):
    tree = defaultdict(dict)
    for deck in decks:
        parts = deck.split("::")
        current = tree
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = {}
    return tree

def preload_stats_loop():
    while True:
        try:
            new_cards = invoke("findCards", query="is:new")
            review_cards = invoke("findCards", query="is:review")
            all_cards = list(set(new_cards + review_cards))
            cards = invoke("cardsInfo", cards=all_cards)
            stats = {}
            for c in cards:
                d = c["deckName"]
                stats.setdefault(d, {"learn": 0, "review": 0})
                if c["cardId"] in new_cards: stats[d]["learn"] += 1
                if c["cardId"] in review_cards: stats[d]["review"] += 1
            cache_data["stats"] = (stats, len(new_cards), len(review_cards))
            cache_data["timestamp"] = time.time()
        except Exception as e:
            print("Error refreshing stats:", e)
        time.sleep(60)

# ------------------- STARTUP -------------------
if not is_anki_running():
    start_anki_silently()
hide_anki_window("anki")
wait_for_ankiconnect()

# Preload stats before opening UI
try:
    new_cards = invoke("findCards", query="is:new")
    review_cards = invoke("findCards", query="is:review")
    all_cards = list(set(new_cards + review_cards))
    cards = invoke("cardsInfo", cards=all_cards)
    stats = {}
    for c in cards:
        d = c["deckName"]
        stats.setdefault(d, {"learn": 0, "review": 0})
        if c["cardId"] in new_cards: stats[d]["learn"] += 1
        if c["cardId"] in review_cards: stats[d]["review"] += 1
    cache_data["stats"] = (stats, len(new_cards), len(review_cards))
    cache_data["timestamp"] = time.time()
except Exception as e:
    print("[!] Initial preload failed:", e)

# ------------------- FLASK APP -------------------

@app.route("/")
def home():
    # Always use cached decks and stats to make UI instant
    stats, learn_today, review_today = cache_data.get("stats", ({}, 0, 0))
    decks = deck_list_cache or []  # No waiting for invoke()
    if not decks:
        try:
            decks = get_cached_decks()
        except Exception:
            decks = []
    deck_tree = build_deck_tree(decks)

    return render_template_string(
        DECK_SELECT_TEMPLATE,
        deck_tree=deck_tree,
        stats=stats,
        learn_today=learn_today,
        review_today=review_today
    )

@app.route("/deck/<deck>")
def deck_view(deck):
    decks = get_cached_decks()
    subdecks = [d for d in decks if d.startswith(deck + "::")]

    if subdecks:
        sub_stats = cache_data["stats"][0] if cache_data["stats"] else {}
        return render_template_string(SUBDECK_TEMPLATE, deck=deck, subdecks=subdecks, stats=sub_stats)
    else:
        # Don't preload cards here — render instantly, JS will fetch later
        return render_template_string(DECK_TEMPLATE, deck=deck, total=0)


@app.route("/api/cards/<deck>")
def api_cards(deck):
    if deck in deck_cache:
        return jsonify(deck_cache[deck])

    try:
        cards = preload_deck(deck)
        deck_cache[deck] = cards
    except Exception as e:
        print("Error loading cards:", e)
        cards = []
    return jsonify(cards)

@app.route("/api/grade/<int:card_id>/<int:ease>")
def api_grade(card_id, ease):
    try:
        invoke("answerCards", answers=[{"cardId": card_id, "ease": ease}])
    except:
        pass
    return {"ok": True}

# ------------------- TEMPLATES -------------------

SUBDECK_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{{ deck }} - Subdecks</title>
<style>
  :root {
    --bg: #0b0c10;
    --card-bg: #101217;
    --primary: #00eaff;
    --text: #e6e6e6;
    --accent: #0ff;
    --good: #00ff9f;
    --hard: #ffb347;
    --again: #ff4b4b;
    --easy: #00c8ff;
    --shadow: 0 0 20px rgba(0, 238, 255, 0.15);
  }

  * {
    box-sizing: border-box;
    transition: all 0.15s ease-in-out;
  }

  body {
    font-family: 'Segoe UI', 'Inter', sans-serif;
    background: radial-gradient(circle at top, #0b0c10, #050608);
    color: var(--text);
    min-height: 100vh;
    margin: 0;
    padding: 40px 25px;
    display: flex;
    flex-direction: column;
    align-items: center;
    overflow-x: hidden;
  }

  h1 {
    color: #0ff;
    text-align: center;
    margin-bottom: 30px;
  }

  .deck-container {
    max-width: 800px;
    margin: 0 auto;
  }

  .deck-header {
    display: grid;
    grid-template-columns: 1fr 100px 100px;
    color: #0ff;
    font-weight: bold;
    border-bottom: 2px solid #0ff;
    padding-bottom: 5px;
    margin-bottom: 10px;
  }

  .deck-button {
    display: grid;
    grid-template-columns: 1fr 100px 100px;
    align-items: center;
    background: #111;
    color: #0ff;
    padding: 15px 20px;
    border-radius: 15px;
    text-decoration: none;
    margin-bottom: 10px;
    transition: background 0.2s ease, transform 0.2s ease;
  }

  .deck-button:hover {
    background: #0ff;
    color: #000;
    transform: translateY(-3px);
  }

  .stats-col { text-align: center; font-weight: bold; }
  a.back { display:block; text-align:center; margin-top:30px; color:#0ff; }
</style>
</head>
<body>

<h1>{{ deck }}</h1>
<div class="deck-container">
  <div class="deck-header">
    <div>Subdeck</div>
    <div>Learn</div>
    <div>Review</div>
  </div>

  {% for sub in subdecks %}
    <a class="deck-button" href="/deck/{{ sub }}">
      <span>{{ sub.split('::')[-1] }}</span>
      <span class="stats-col">{{ stats[sub].learn }}</span>
      <span class="stats-col">{{ stats[sub].review }}</span>
    </a>
  {% endfor %}
</div>

<a class="back" href="/">⬅ Back to Main Decks</a>
</body>
</html>
"""



DECK_SELECT_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Decks Overview</title>
<style>
  :root {
    --bg: #0b0c10;
    --card-bg: #11141a;
    --primary: #00eaff;
    --text: #e6e6e6;
    --accent: #0ff;
    --good: #00ff9f;
    --hard: #ffb347;
    --again: #ff4b4b;
    --easy: #00c8ff;
    --shadow: 0 0 25px rgba(0, 238, 255, 0.15);
    --transition: 0.2s cubic-bezier(0.25, 0.8, 0.25, 1);
  }

  * { box-sizing: border-box; transition: all var(--transition); }

  body {
    font-family: 'Inter', 'Segoe UI', sans-serif;
    background: radial-gradient(circle at top, #0b0c10, #050608 80%);
    color: var(--text);
    min-height: 100vh;
    margin: 0;
    padding: 40px 25px;
    display: flex;
    flex-direction: column;
    align-items: center;
    overflow-x: hidden;
  }

  h1 {
    display: flex;
    justify-content: space-between;
    align-items: center;
    color: var(--primary);
    font-size: 2.3rem;
    text-shadow: 0 0 10px rgba(0,255,255,0.2);
  }

  h1 span.stats-bar {
    font-size: 1rem;
    background: rgba(0,255,255,0.1);
    padding: 10px 16px;
    border-radius: 10px;
    box-shadow: 0 0 10px rgba(0,255,255,0.15);
  }

  .deck-container {
    display: flex;
    flex-direction: column;
    gap: 15px;
    max-width: 800px;
    width: 100%;
    margin-top: 30px;
  }

  .deck-header {
    display: grid;
    grid-template-columns: 1fr 100px 100px;
    color: var(--primary);
    font-weight: bold;
    margin-bottom: 10px;
    border-bottom: 2px solid var(--primary);
    padding-bottom: 5px;
  }

  .deck-button, .deck-group {
    display: grid;
    grid-template-columns: 1fr 100px 100px;
    align-items: center;
    background: var(--card-bg);
    color: var(--primary);
    padding: 18px 22px;
    border-radius: 16px;
    text-decoration: none;
    box-shadow: var(--shadow);
    border: 1px solid rgba(0,255,255,0.1);
    font-weight: 500;
    transition: transform 0.2s ease, box-shadow 0.3s ease, background 0.3s ease;
  }

  .deck-button:hover, .deck-group:hover {
    transform: translateY(-4px) scale(1.01);
    background: linear-gradient(90deg, var(--primary), #00ffc6);
    color: #000;
    box-shadow: 0 0 20px rgba(0,255,255,0.35);
  }

  .stats-col {
    text-align: center;
    font-weight: bold;
    color: var(--text);
  }

  .subdeck-container {
    display: none;
    margin-left: 30px;
  }

  /* Floating subtle glow effect for cards */
  @keyframes floatGlow {
    0%,100% { box-shadow: 0 0 20px rgba(0,238,255,0.15); }
    50% { box-shadow: 0 0 30px rgba(0,238,255,0.25); }
  }

  .deck-button {
    animation: floatGlow 4s ease-in-out infinite;
  }

  /* Mobile-friendly adjustments */
  @media (max-width: 600px) {
    h1 { font-size: 1.8rem; flex-direction: column; gap: 10px; }
    h1 span.stats-bar { font-size: 0.95rem; padding: 8px 12px; }
    .deck-header { grid-template-columns: 1fr 80px 80px; }
    .deck-button, .deck-group { grid-template-columns: 1fr 80px 80px; padding: 14px 18px; }
  }
</style>
</head>
<body>

<h1>My Decks</h1>

<div class="deck-container">
  <div class="deck-header">
    <div>Deck Name</div>
    <div>Learn</div>
    <div>Review</div>
  </div>

  {% for main, subs in deck_tree.items() if '::' not in main %}
    <a class="deck-button" href="/deck/{{ main }}">
      <span>{{ main }}</span>
      <span class="stats-col">{{ stats[main].learn if main in stats else 0 }}</span>
      <span class="stats-col">{{ stats[main].review if main in stats else 0 }}</span>
    </a>
  {% endfor %}
</div>

</body>
</html>
"""

DECK_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Deck: {{ deck }}</title>
<style>
  :root {
    --bg: #0b0c10;
    --card-bg: #11141a;
    --primary: #00eaff;
    --text: #e6e6e6;
    --accent: #0ff;
    --good: #00ff9f;
    --hard: #ffb347;
    --again: #ff4b4b;
    --easy: #00c8ff;
    --shadow: 0 0 25px rgba(0, 238, 255, 0.15);
    --transition: 0.2s cubic-bezier(0.25, 0.8, 0.25, 1);
  }

  * { box-sizing: border-box; transition: all var(--transition); }

  body {
    font-family: 'Inter', 'Segoe UI', sans-serif;
    background: radial-gradient(circle at top, #0b0c10, #050608 80%);
    color: var(--text);
    min-height: 100vh;
    margin: 0;
    padding: 40px 25px;
    display: flex;
    flex-direction: column;
    align-items: center;
    overflow-x: hidden;
  }

  .card {
    background: linear-gradient(145deg, var(--card-bg), #0d0f14);
    padding: 40px;
    border-radius: 22px;
    width: 100%;
    max-width: 700px;
    text-align: center;
    margin-bottom: 40px;
    box-shadow: 0 8px 25px rgba(0,0,0,0.65), var(--shadow);
    border: 1px solid rgba(255, 255, 255, 0.05);
    animation: floatGlow 4s ease-in-out infinite;
  }

  .card:hover {
    transform: translateY(-6px) scale(1.01);
    box-shadow: 0 15px 40px rgba(0,0,0,0.75), 0 0 25px rgba(0, 238, 255, 0.1);
  }

  #progress {
    width: 100%;
    height: 14px;
    background: #1a1d24;
    border-radius: 10px;
    margin-bottom: 30px;
    overflow: hidden;
    position: relative;
  }

  #progress-bar {
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, var(--primary), var(--good));
    border-radius: 10px;
    box-shadow: 0 0 10px var(--primary);
    transition: width var(--transition);
  }

  h2 {
    margin-bottom: 18px;
    font-size: 2.3rem;
    letter-spacing: 1.2px;
    color: var(--primary);
    text-shadow: 0 0 10px rgba(0,238,255,0.2);
  }

  h3 {
    margin-bottom: 28px;
    font-size: 1.45rem;
    word-wrap: break-word;
    line-height: 1.6;
    color: #f1f1f1;
    min-height: 60px;
  }

  input[type=text] {
    width: 90%;
    max-width: 600px;
    padding: 15px 18px;
    font-size: 1.15rem;
    border-radius: 12px;
    border: 1px solid rgba(255,255,255,0.08);
    margin-bottom: 22px;
    outline: none;
    background: #1a1c22;
    color: var(--text);
    transition: all 0.2s ease;
  }

  input[type=text]:focus {
    border-color: var(--primary);
    box-shadow: 0 0 12px rgba(0,238,255,0.5);
  }

  .buttons {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 14px;
    margin-bottom: 25px;
  }

  .buttons button {
    padding: 12px 25px;
    font-size: 1rem;
    border-radius: 12px;
    border: none;
    cursor: pointer;
    font-weight: 600;
    color: #000;
    box-shadow: 0 3px 10px rgba(0,0,0,0.3);
    transition: all 0.15s ease;
  }

  .buttons button:hover {
    transform: translateY(-4px) scale(1.05);
    filter: brightness(1.15);
  }

  .again { background: var(--again); }
  .hard { background: var(--hard); }
  .good { background: var(--good); }
  .easy { background: var(--easy); }

  .correct, .wrong {
    font-weight: bold;
    font-size: 1.2rem;
    animation: pop 0.25s ease-out;
  }

  .correct { color: var(--good); }
  .wrong { color: var(--again); }

  a {
    color: var(--primary);
    margin-top: 25px;
    display: inline-block;
    text-decoration: none;
    font-weight: 600;
    letter-spacing: 0.5px;
    transition: all 0.2s ease;
  }

  a:hover {
    color: var(--accent);
    text-shadow: 0 0 8px var(--accent);
  }

  @keyframes pop {
    0% { transform: scale(0.8); opacity: 0; }
    60% { transform: scale(1.05); opacity: 1; }
    100% { transform: scale(1); }
  }

  @keyframes floatGlow {
    0%,100% { box-shadow: 0 0 20px rgba(0,238,255,0.15); }
    50% { box-shadow: 0 0 30px rgba(0,238,255,0.25); }
  }

  /* Mobile adjustments */
  @media (max-width: 600px) {
    .card { padding: 28px; border-radius: 18px; }
    h2 { font-size: 1.8rem; }
    h3 { font-size: 1.25rem; }
    input[type=text] { font-size: 1rem; padding: 12px; }
    .buttons button { padding: 10px 20px; font-size: 0.95rem; }
  }
</style>
</head>
<body>

<div class="card">
  <div id="progress"><div id="progress-bar"></div></div>
  <h2 id="deck-title">Deck: {{ deck }}</h2>
  <h3 id="question">Loading...</h3>
  <input type="text" id="answer" placeholder="Type your answer..." autofocus>
  <div class="buttons" id="grade-buttons" style="display:none;">
    <button class="again" onclick="gradeCard(1)">Again</button>
    <button class="hard" onclick="gradeCard(2)">Hard</button>
    <button class="good" onclick="gradeCard(3)">Good</button>
    <button class="easy" onclick="gradeCard(4)">Easy</button>
  </div>
  <div id="feedback" style="margin-top:15px;font-size:1rem;"></div>
  <a href="/">⬅ Change deck</a>
</div>

<script>
let cards=[], total=0, currentIndex=0, currentCard=null;

async function loadCards(){
    const res = await fetch("/api/cards/{{ deck }}");
    cards = await res.json();
    total = cards.length;
    loadNextCard();
}

function updateProgress(){
    document.getElementById("progress-bar").style.width = ((currentIndex/total)*100)+"%";
}

function loadNextCard(){
    if(currentIndex >= total){
        document.getElementById("question").textContent = "Finished all cards!";
        document.getElementById("answer").style.display="none";
        document.getElementById("grade-buttons").style.display="none";
        document.getElementById("feedback").textContent = "Redirecting to main deck list...";
        setTimeout(() => {
            window.location.href = "/";
        }, 1000); // wait 2 seconds before redirect
        return;
    }

    currentCard = cards[currentIndex];
    document.getElementById("question").textContent = currentCard.question;
    document.getElementById("answer").value = "";
    document.getElementById("answer").style.display = "inline-block";
    document.getElementById("grade-buttons").style.display="none";
    document.getElementById("feedback").textContent = "";
    document.getElementById("answer").focus();
    updateProgress();
}


function fuzzyMatch(a,b){ return a.toLowerCase().trim()===b.toLowerCase().trim(); }

function checkAnswer(){
    const typed = document.getElementById("answer").value.trim();
    if(!typed) return;
    if(fuzzyMatch(typed,currentCard.answer)){
        document.getElementById("feedback").innerHTML = "<span class='correct'>Correct!</span>";
        gradeCard(3);
    } else {
        document.getElementById("feedback").innerHTML = "<span class='wrong'>Wrong. Correct: "+currentCard.answer+"</span>";
        document.getElementById("grade-buttons").style.display="flex";
    }
}

function gradeCard(ease){
    fetch(`/api/grade/${currentCard.card_id}/${ease}`);
    currentIndex++;
    loadNextCard();
}

document.getElementById("answer").addEventListener("keyup", e => {
    if(e.key==="Enter") checkAnswer();
});

document.getElementById("answer").addEventListener("keydown", e => {
    if(document.getElementById("grade-buttons").style.display==="flex"){
        if(["1","2","3","4"].includes(e.key)){
            e.preventDefault();
            gradeCard(parseInt(e.key));
        }
    }
});

loadCards();
</script>
</body>
</html>

"""
def start_flask():
    app.run(debug=False, threaded=True)

if __name__ == "__main__":
    threading.Thread(target=preload_stats_loop, daemon=True).start()
    threading.Thread(target=start_flask, daemon=True).start()

    userdata = os.path.join(tempfile.gettempdir(), "anki_webview_userdata")
    os.makedirs(userdata, exist_ok=True)

    webview.create_window("Anki Server", "http://127.0.0.1:5000")
    webview.start(gui='edgechromium', debug=False, http_server=True)
