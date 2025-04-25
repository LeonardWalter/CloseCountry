import json
import random
import sqlite3
import uuid
import os
import sys
import geopandas as gpd
import re

from flask import Flask, render_template, jsonify, request, session, g
from shapely.ops import unary_union, nearest_points
from shapely.geometry import LineString, mapping
from pyproj import Geod
from typing import Dict, Optional, List, Set, FrozenSet
from dotenv import load_dotenv

# --- Configuration & Constants ---
DATABASE_FILE = 'highscores.db'
LEADERBOARD_SIZE = 5
DISTANCES_DATA_FILE = 'distances.jsonl'
GEOJSON_SHAPES_FILE = 'world.geo.json'
COUNTRY_CODES_FILE = 'country_codes.json'
HIGHSCORE_FILE = 'closer_country_highscore.txt'
SHAPES_NAME_COLUMN = 'name_en'
SHAPES_CODE_COLUMN = 'iso_a2'
SHAPES_TYPE_COLUMN = 'type'
VALID_TYPES = ["Country", "Sovereign country"]
GEOMETRY_SIMPLIFY_TOLERANCE = 0.05

load_dotenv()
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY')
DATABASE_FILE = os.environ.get('DATABASE_FILE_PATH', 'highscores.db')

if not app.secret_key:
    app.logger.critical("FATAL: SECRET_KEY environment variable not set!")
# app.logger.setLevel(logging.DEBUG)

# --- Global Data Storage ---
distances_data: Optional[Dict[FrozenSet[str], float]] = None
country_list: Optional[List[str]] = None
country_code_map: Optional[Dict[str, str]] = None
gdf_all_shapes_preload: Optional[gpd.GeoDataFrame] = None

def load_country_mapping_from_json(mapping_filepath: str) -> Optional[Dict[str, str]]:
    """Loads mapping from the pre-processed JSON file."""
    mapping: Dict[str, str] = {}
    app.logger.info(f"Loading country code mapping from {mapping_filepath}...")
    if not os.path.exists(mapping_filepath):
        app.logger.error(f"Mapping file '{mapping_filepath}' not found.")
        return None
    try:
        with open(mapping_filepath, 'r', encoding='utf-8') as f:
            mapping_list = json.load(f)

        processed_names = set()
        for item in mapping_list:
            name = item.get('name')
            code = item.get('code')
            if name and code and name not in processed_names: # Ensure data exists and name is unique
                mapping[name] = code
                processed_names.add(name)

        app.logger.info(f"Created mapping for {len(mapping)} countries from JSON.")
        return mapping if mapping else None

    except json.JSONDecodeError as e:
        app.logger.error(f"Error decoding JSON from '{mapping_filepath}': {e}", exc_info=True)
        return None
    except Exception as e:
        app.logger.error(f"Error reading mapping file '{mapping_filepath}': {e}", exc_info=True)
        return None

def get_db():
    """Connects to the specific database."""
    if 'db' not in g:
        g.db = sqlite3.connect( DATABASE_FILE, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)
    if db is not None: db.close()

def init_db():
    """Initializes the database; creates tables if they don't exist."""
    try:
        db = sqlite3.connect(DATABASE_FILE)
        cursor = db.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS highscores (user_id TEXT PRIMARY KEY, highscore INTEGER NOT NULL DEFAULT 0)")
        cursor.execute("CREATE TABLE IF NOT EXISTS leaderboard (user_id TEXT PRIMARY KEY, nickname TEXT, score INTEGER NOT NULL DEFAULT 0)")
        db.commit()
        db.close()
    except Exception as e:
        app.logger.error(f"Failed during database initialization: {e}", exc_info=True)

def load_processed_shapes(
    shapes_filepath: str,
    name_col: str,
    type_col: str,
    valid_types: List[str]
) -> Optional[gpd.GeoDataFrame]:
    """Loads, filters, validates CRS, and preprocesses the shapes GeoDataFrame."""
    app.logger.info(f"Loading and processing shapes from {shapes_filepath}...")
    if not os.path.exists(shapes_filepath):
        app.logger.error(f"Shapes file '{shapes_filepath}' not found for preprocessing.")
        return None
    try:
        gdf = gpd.read_file(shapes_filepath)
        app.logger.info(f"Loaded {len(gdf)} raw shape features.")

        # Validate essential columns
        if name_col not in gdf.columns or type_col not in gdf.columns:
            app.logger.error(f"Required columns ('{name_col}', '{type_col}') missing in shapes file.")
            return None

        # Ensure CRS is geographic
        if gdf.crs is None:
            app.logger.warning("Shapes file CRS missing. Assuming EPSG:4326 (WGS84).")
            gdf.crs = 'EPSG:4326'
        elif not gdf.crs.is_geographic:
            app.logger.warning(f"Shapes file CRS '{gdf.crs.name}' is projected. Reprojecting to EPSG:4326.")
            try:
                gdf = gdf.to_crs('EPSG:4326')
            except Exception as crs_e:
                app.logger.error(f"Failed reprojecting shapes file CRS: {crs_e}", exc_info=True)
                return None

        # Filter by valid types
        gdf_filtered = gdf[gdf[type_col].isin(valid_types)].copy()
        app.logger.info(f"Filtered down to {len(gdf_filtered)} features matching valid types.")

        # Filter out rows with missing/invalid names or geometry
        initial_count = len(gdf_filtered)
        gdf_filtered = gdf_filtered.dropna(subset=[name_col, 'geometry'])
        gdf_filtered = gdf_filtered[gdf_filtered.geometry.is_valid]
        gdf_filtered = gdf_filtered[~gdf_filtered.geometry.is_empty]
        final_count = len(gdf_filtered)
        if final_count < initial_count:
            app.logger.debug(f"Removed {initial_count - final_count} features due to missing name/geometry or invalid geometry.")

        if gdf_filtered.empty:
            app.logger.error("No valid, named country shapes found after filtering.")
            return None

        # Optional: Set index for potentially faster lookups later
        try:
            if gdf_filtered.index.name != name_col and gdf_filtered[name_col].is_unique:
                gdf_filtered = gdf_filtered.set_index(name_col)
                app.logger.info(f"Set index of shapes cache to '{name_col}'.")
            elif gdf_filtered.index.name == name_col:
                app.logger.info(f"Shapes cache index already set to '{name_col}'.")
            else:
                app.logger.warning(f"Cannot set index to '{name_col}' (not unique?). Proceeding without index.")

        except Exception as idx_e:
            app.logger.warning(f"Could not set index on shapes cache: {idx_e}")


        app.logger.info(f"Shape preprocessing complete. Cached {len(gdf_filtered)} valid features.")
        return gdf_filtered

    except Exception as e:
        app.logger.error(f"Error loading/processing shapes file '{shapes_filepath}': {e}", exc_info=True)
        return None

def load_distances_and_countries(filepath: str) -> tuple[Optional[Dict[FrozenSet[str], float]], Optional[List[str]]]:
    distances: Dict[FrozenSet[str], float] = {}
    countries_set: Set[str] = set()
    if not os.path.exists(filepath): return None, None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    data = json.loads(line)
                    c1, c2, dist = data['country1'], data['country2'], data['distance_km']
                    pair = frozenset([c1, c2])
                    if pair not in distances or dist < distances[pair]: distances[pair] = dist
                    countries_set.add(c1); countries_set.add(c2)
                except Exception: pass # Ignore bad lines
        clist = sorted(list(countries_set))
        return (distances, clist) if len(clist) >= 3 else (None, None)
    except Exception as e: app.logger.error(f"Err dist load: {e}", file=sys.stderr); return None, None

def get_user_highscore(user_id: str) -> int:
    """Gets the high score for a specific user ID."""
    db = get_db()
    cur = db.execute("SELECT highscore FROM highscores WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    return row['highscore'] if row else 0

def update_user_highscore(user_id: str, score: int):
    """Updates or inserts the high score for a user ID."""
    db = get_db()
    try:
        with db:
            db.execute(
                "INSERT INTO highscores (user_id, highscore) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET highscore = excluded.highscore "
                "WHERE excluded.highscore > highscores.highscore", # Only update if new score is higher
                (user_id, score)
            )
    except Exception as e:
        app.logger.error(f"Failed to update highscore for user {user_id}: {e}", exc_info=True)

def update_leaderboard(user_id: str, nickname: str, score: int):
    """Inserts or updates a user's score and nickname on the leaderboard."""
    db = get_db()
    if not nickname or len(nickname) > 20: # Limit nickname length
        nickname = "Anonymous"
    # Remove potentially harmful characters
    nickname = re.sub(r'[^\w\s-]', '', nickname).strip()
    if not nickname: nickname = "Anonymous"

    app.logger.info(f"Updating leaderboard: User={user_id}, Nickname='{nickname}', Score={score}")
    try:
        with db:
            # Insert new entry or replace existing entry for the user_id.
            db.execute(
                """
                INSERT INTO leaderboard (user_id, nickname, score) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    nickname = excluded.nickname,
                    score = excluded.score
                WHERE excluded.score >= leaderboard.score
                """,
                (user_id, nickname, score)
            )
    except Exception as e:
        app.logger.error(f"Failed to update leaderboard for user {user_id}: {e}", exc_info=True)

def get_top_scores(limit: int = 5) -> List[Dict]:
    """Gets the top N scores from the leaderboard."""
    db = get_db()
    try:
        cur = db.execute(
            "SELECT nickname, score FROM leaderboard ORDER BY score DESC LIMIT ?",
            (limit,)
        )
        rows = cur.fetchall()
        return [{"nickname": row["nickname"], "score": row["score"]} for row in rows]
    except Exception as e:
        app.logger.error(f"Failed to fetch top scores: {e}", exc_info=True)
        return []

def get_distance(c1: str, c2: str) -> Optional[float]:
    global distances_data
    if distances_data is None: return None
    return distances_data.get(frozenset([c1, c2]))


db_dir = os.path.dirname(DATABASE_FILE)
if db_dir and not os.path.exists(db_dir):
    try:
        os.makedirs(db_dir)
        app.logger.info(f"Created database directory: {db_dir}")
    except OSError as e:
        app.logger.error(f"Could not create database directory {db_dir}: {e}", exc_info=True)

app.logger.info("Starting initial data loading...")
init_db()
distances_data, country_list_from_distances = load_distances_and_countries(DISTANCES_DATA_FILE)
country_code_map = load_country_mapping_from_json(COUNTRY_CODES_FILE)
gdf_all_shapes_preload = load_processed_shapes(GEOJSON_SHAPES_FILE, SHAPES_NAME_COLUMN, SHAPES_TYPE_COLUMN, VALID_TYPES) 

# Validate and determine final playable list
if distances_data is None or country_list_from_distances is None or country_code_map is None:
    app.logger.critical("FATAL: Failed to load essential game data during startup. Exiting process might occur depending on WSGI server.")
    distances_data = {}; country_list = []; country_code_map = {}; gdf_all_shapes_preload = gpd.GeoDataFrame() 
else:
    country_list = sorted([
        name for name in country_list_from_distances
        if name in country_code_map and name in gdf_all_shapes_preload.index 
    ])
    app.logger.info(f"Successfully determined {len(country_list)} playable countries.")
    if len(country_list) < 3:
        app.logger.critical("FATAL: Fewer than 3 playable countries available after loading.")


def ensure_user_id():
    """Checks for user_id in session, creates if missing."""
    if 'user_id' not in session:
        new_id = uuid.uuid4().hex
        session['user_id'] = new_id
        session.modified = True 
        app.logger.info(f"New user session started with ID: {new_id}")

def get_user_nickname(user_id: str) -> Optional[str]:
    """Gets the existing nickname for a user ID from the leaderboard table."""
    db = get_db()
    try:
        cur = db.execute("SELECT nickname FROM leaderboard WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return row['nickname'] if row and row['nickname'] else None
    except Exception as e:
        app.logger.error(f"Failed to fetch nickname for user {user_id}: {e}", exc_info=True)
        return None

@app.route('/')
def index():
    ensure_user_id()
    user_highscore = get_user_highscore(session['user_id'])
    app.logger.info(f"DEBUG user highscore got: {user_highscore} for: {session['user_id']}")
    session['user_highscore'] = user_highscore
    session.modified = True
    return render_template('index.html', highscore=user_highscore)

@app.route('/start_round', methods=['GET'])
def start_round():
    ensure_user_id()
    user_id = session['user_id']
    user_highscore = session.get('user_highscore', get_user_highscore(user_id))
    current_score = session.get('score', 0)

    global country_list, country_code_map
    if session.get('round_in_progress', False) and \
       'served_base_name' in session and \
       'served_target1_name' in session and \
       'served_target2_name' in session:

        base_name = session['served_base_name']
        t1_name = session['served_target1_name']
        t2_name = session['served_target2_name']

        base_code = country_code_map.get(base_name)
        t1_code = country_code_map.get(t1_name)
        t2_code = country_code_map.get(t2_name)
        next_t1_code = session.get('served_next_t1_code')
        next_t2_code = session.get('served_next_t2_code')

        if not all([base_code, t1_code, t2_code]):
            app.logger.error("Failed to re-serve round: Missing codes for stored countries.")
            session['round_in_progress'] = False
        else:
            return jsonify({
                'base_country': {'name': base_name, 'code': base_code },
                'target1': {'name': t1_name, 'code': t1_code},
                'target2': {'name': t2_name, 'code': t2_code},
                'score': current_score,
                'highscore': user_highscore,
                'next_target1_code': next_t1_code,
                'next_target2_code': next_t2_code,
            })

    if 'score' not in session:
        session['score'] = 0
        current_score = 0
        app.logger.debug(f"GET /start_round: Score missing, reset to 0 for user {user_id}")

    if not country_list or not country_code_map:
        return jsonify({'error': 'Game data not fully loaded'}), 500

    current_base_name = None
    current_target1_name = None
    current_target2_name = None
    used_prefetched = False

    if 'next_round_base' in session and 'next_round_t1' in session and 'next_round_t2' in session:
        potential_base = session['next_round_base']
        potential_t1 = session['next_round_t1']
        potential_t2 = session['next_round_t2']
        if potential_base in country_code_map and potential_t1 in country_code_map and potential_t2 in country_code_map and \
           potential_t1 != potential_base and potential_t2 != potential_base and potential_t1 != potential_t2:
            current_base_name = potential_base
            current_target1_name = potential_t1
            current_target2_name = potential_t2
            used_prefetched = True
            app.logger.debug(f"Using prefetched round data: Base={current_base_name}, T1={current_target1_name}, T2={current_target2_name}")
        else:
            app.logger.warning("Invalid prefetched round data found, generating fresh.")
        session.pop('next_round_base', None); session.pop('next_round_t1', None); session.pop('next_round_t2', None)

    if not used_prefetched:
        app.logger.debug("Generating fresh round data.")
        valid_start_countries = [c for c in country_list if c in country_code_map]
        if not valid_start_countries:
            return jsonify({'error': 'No countries with codes found'}), 500

        if 'base_country' in session and session['base_country'] in country_code_map:
            current_base_name = session['base_country'] # Could be resuming after error/mismatch
            app.logger.debug(f"Generating fresh round using existing base_country from session: {current_base_name}")
        else:
            current_base_name = random.choice(valid_start_countries)
            app.logger.debug(f"Starting new game sequence, random base: {current_base_name}")

        possible_targets = [c for c in country_list if c != current_base_name and c in country_code_map]
        if len(possible_targets) < 2:
            session.pop('score', None); session.pop('base_country', None); session.pop('round_in_progress', None) # Clear state
            session.modified = True
            return jsonify({'game_over': True, 'message': 'Not enough valid countries with flags!'}), 200
        current_target1_name, current_target2_name = random.sample(possible_targets, 2)

    current_base_code = country_code_map.get(current_base_name)
    current_target1_code = country_code_map.get(current_target1_name)
    current_target2_code = country_code_map.get(current_target2_name)

    if not all([current_base_code, current_target1_code, current_target2_code]):
        app.logger.error(f"CODE LOOKUP FAILED for current round")
        session.pop('score', None); session.pop('base_country', None); session.pop('round_in_progress', None)
        session.modified = True
        return jsonify({'error': 'Internal error: Could not find codes for current round countries.'}), 500

    session['base_country'] = current_base_name # Needed for make_guess validation
    session['user_highscore'] = user_highscore

    next_round_base_name = None; next_round_t1_name = None; next_round_t2_name = None
    next_round_t1_code = None; next_round_t2_code = None
    dist_1 = get_distance(current_base_name, current_target1_name)
    dist_2 = get_distance(current_base_name, current_target2_name)

    if dist_1 is not None and dist_2 is not None:
        potential_next_base = current_target1_name if dist_1 <= dist_2 else current_target2_name
        possible_next_targets = [c for c in country_list if c != potential_next_base and c in country_code_map]
        if len(possible_next_targets) >= 2:
            next_round_base_name = potential_next_base
            next_round_t1_name, next_round_t2_name = random.sample(possible_next_targets, 2)
            next_round_t1_code = country_code_map.get(next_round_t1_name)
            next_round_t2_code = country_code_map.get(next_round_t2_name)
            if next_round_t1_code and next_round_t2_code:
                session['next_round_base'] = next_round_base_name
                session['next_round_t1'] = next_round_t1_name
                session['next_round_t2'] = next_round_t2_name
            else:
                app.logger.warning(f"Could not find codes for next round targets, prefetch data not stored.")
                next_round_t1_code = None; next_round_t2_code = None
        else: app.logger.info("Not enough countries for next round prefetch.")
    else: app.logger.error(f"Missing distance for current round pairs, cannot determine next base.")

    session['served_base_name'] = current_base_name
    session['served_target1_name'] = current_target1_name
    session['served_target2_name'] = current_target2_name
    session['served_next_t1_code'] = next_round_t1_code
    session['served_next_t2_code'] = next_round_t2_code
    session['round_in_progress'] = True
    app.logger.debug(f"Serving round: Base={current_base_name}, T1={current_target1_name}, T2={current_target2_name}. Flag set.")

    session.modified = True
    return jsonify({
        'base_country': {'name': current_base_name, 'code': current_base_code },
        'target1': {'name': current_target1_name, 'code': current_target1_code},
        'target2': {'name': current_target2_name, 'code': current_target2_code},
        'score': current_score,
        'highscore': user_highscore,
        'next_target1_code': next_round_t1_code,
        'next_target2_code': next_round_t2_code,
    })

@app.route('/make_guess', methods=['POST'])
def make_guess():
    ensure_user_id()
    user_id = session['user_id']
    current_user_highscore = get_user_highscore(user_id)

    global distances_data
    if distances_data is None: return jsonify({'error': 'Distance data not loaded'}), 500
    data = request.get_json()
    if not data or 'base_country_name' not in data or 'chosen_country_name' not in data or 'other_country_name' not in data: 
        return jsonify({'error': 'Missing name data in guess request'}), 400
    base_c, chosen_c, other_c = data['base_country_name'], data['chosen_country_name'], data['other_country_name']

    expected_base = session.get('base_country') # This was set by the /start_round that served the round
    if not expected_base or base_c != expected_base:
        app.logger.warning(f"Guess received for base '{base_c}' but expected '{expected_base}' in session.")
        session.pop('score', None)
        session.pop('base_country', None)
        session.pop('next_round_base', None); session.pop('next_round_t1', None); session.pop('next_round_t2', None)
        session.pop('served_base_name', None); session.pop('served_target1_name', None); session.pop('served_target2_name', None)
        session.pop('served_next_t1_code', None); session.pop('served_next_t2_code', None)
        session.pop('round_in_progress', None)
        session.modified = True
        return jsonify({'error': 'Round data mismatch. Please start again.', 'correct': False, 'game_over': True, 'final_score': session.get('score', 0)}), 400

    session.pop('round_in_progress', None)
    session.pop('served_base_name', None); session.pop('served_target1_name', None); session.pop('served_target2_name', None)
    session.pop('served_next_t1_code', None); session.pop('served_next_t2_code', None)
    app.logger.debug("Cleared anti-refresh flag for served round.")

    dist_chosen, dist_other = get_distance(base_c, chosen_c), get_distance(base_c, other_c)
    if dist_chosen is None or dist_other is None: return jsonify({'error': f'Internal error: Missing distance data for {base_c}.'}), 500

    is_correct_distance = (dist_chosen <= dist_other)
    closer_country = chosen_c if is_correct_distance else other_c
    is_correct_guess = (is_correct_distance and chosen_c == closer_country)

    response_data = {'correct': is_correct_guess, 'chosen_dist': round(dist_chosen, 1), 'other_dist': round(dist_other, 1), 'closer_country': closer_country}
    current_score = session.get('score', 0)

    if is_correct_guess:
        current_score += 1
        session['score'] = current_score
        response_data['score'] = current_score

        if current_score > current_user_highscore:
            app.logger.info(f"New high score for user {user_id}: {current_score}")
            update_user_highscore(user_id, current_score)
            session['user_highscore'] = current_score
            response_data['new_highscore'] = True
        else:
            response_data['new_highscore'] = False
        response_data['highscore'] = session.get('user_highscore', current_user_highscore)

    else: # Incorrect Guess
        final_score = current_score
        response_data['game_over'] = True
        response_data['final_score'] = final_score
        response_data['highscore'] = current_user_highscore # Highscore before this game
        response_data['map_available'] = True
        response_data['map_params'] = {'base': base_c, 't1': chosen_c, 't2': other_c}

        if final_score > 0 and final_score >= current_user_highscore:
            response_data['prompt_nickname'] = True
            existing_nickname = get_user_nickname(user_id)
            response_data['existing_nickname'] = existing_nickname
        else:
            response_data['prompt_nickname'] = False

        session.pop('score', None)
        session.pop('base_country', None)
        session.pop('next_round_base', None)
        session.pop('next_round_t1', None)
        session.pop('next_round_t2', None)
        session['last_final_score'] = final_score

    session.modified = True
    return jsonify(response_data)

@app.route('/get_game_over_data')
def get_game_over_data_route():
    global gdf_all_shapes_preload # Access preloaded shapes

    base_c = request.args.get('base')
    target_c1 = request.args.get('t1')
    target_c2 = request.args.get('t2')

    if not all([base_c, target_c1, target_c2]):
        return jsonify({"error": "Missing country parameters"}), 400

    # Use preloaded shapes data
    if gdf_all_shapes_preload is None or gdf_all_shapes_preload.empty:
        app.logger.error("Map data requested, but preloaded shapes data is not available.")
        return jsonify({"error": "Internal Server Error: Shape data unavailable"}), 500
    # Make a copy to avoid modifying the global preload if needed for filtering etc.
    gdf_shapes = gdf_all_shapes_preload.copy()

    # Check name column just in case
    if SHAPES_NAME_COLUMN not in gdf_shapes.columns and gdf_shapes.index.name != SHAPES_NAME_COLUMN:
        app.logger.error(f"Name identifier '{SHAPES_NAME_COLUMN}' not found as column or index in preloaded shapes.")
        return jsonify({"error": "Internal Server Error: Shape configuration"}), 500

    features = []
    involved_countries = [base_c, target_c1, target_c2]
    geoms = {}
    colors = {base_c: 'dodgerblue', target_c1: 'orangered', target_c2: 'orangered'}

    try:
        if gdf_shapes.index.name == SHAPES_NAME_COLUMN:
            # Use .loc for index lookup. Need to check if all keys exist first for robustness.
            missing_countries = [c for c in involved_countries if c not in gdf_shapes.index]
            if missing_countries:
                app.logger.warning(f"Could not find all requested countries in shapes index: Missing {missing_countries}")
                return jsonify({"error": f"Could not find shape data for countries: {missing_countries}"}), 404
            gdf_involved = gdf_shapes.loc[involved_countries]
            iterator = gdf_involved.iterrows()
        else:
            # Use boolean mask for column lookup
            gdf_involved = gdf_shapes[gdf_shapes[SHAPES_NAME_COLUMN].isin(involved_countries)]
            if len(gdf_involved) != len(involved_countries):
                found_names = gdf_involved[SHAPES_NAME_COLUMN].tolist()
                missing = set(involved_countries) - set(found_names)
                app.logger.warning(f"Could not find all requested countries in shapes column: Missing {missing}")
                return jsonify({"error": f"Could not find shape data for countries: {missing}"}), 404
            iterator = gdf_involved.iterrows()

        for index, row in iterator:
            country_name = index if gdf_shapes.index.name == SHAPES_NAME_COLUMN else row[SHAPES_NAME_COLUMN]
            current_geom = row.geometry
            if not current_geom or not current_geom.is_valid or current_geom.is_empty:
                app.logger.warning(f"Skipping invalid geometry for {country_name} found in cache during map generation.")
                continue

            geom_orig = unary_union(current_geom)
            geoms[country_name] = geom_orig

            geom_simplified = geom_orig.simplify(GEOMETRY_SIMPLIFY_TOLERANCE, preserve_topology=True)
            # Add the *simplified* geometry to the GeoJSON output
            features.append({
                "type": "Feature",
                "geometry": mapping(geom_simplified),
                "properties": {
                    "name": country_name,
                    "feature_type": "country_shape",
                    "color": colors.get(country_name, 'grey')
                }
            })

        if len(geoms) != 3: return jsonify({"error": "Could not find shapes for all countries"}), 404

        # Calculate nearest points using ORIGINAL geometries for accuracy
        base_geom, t1_geom, t2_geom = geoms[base_c], geoms[target_c1], geoms[target_c2]
        base_pt_vs_t1, t1_pt = nearest_points(base_geom, t1_geom)
        base_pt_vs_t2, t2_pt = nearest_points(base_geom, t2_geom)

        geod = Geod(ellps="WGS84")
        _, _, dist1_m = geod.inv(base_pt_vs_t1.x, base_pt_vs_t1.y, t1_pt.x, t1_pt.y)
        _, _, dist2_m = geod.inv(base_pt_vs_t2.x, base_pt_vs_t2.y, t2_pt.x, t2_pt.y)

        # Add features
        features.append({"type": "Feature", "geometry": mapping(base_pt_vs_t1), "properties": {"feature_type": "point", "name": f"{base_c} (near {target_c1})"}})
        features.append({"type": "Feature", "geometry": mapping(t1_pt), "properties": {"feature_type": "point", "name": target_c1}})
        features.append({"type": "Feature", "geometry": mapping(base_pt_vs_t2), "properties": {"feature_type": "point", "name": f"{base_c} (near {target_c2})"}})
        features.append({"type": "Feature", "geometry": mapping(t2_pt), "properties": {"feature_type": "point", "name": target_c2}})
        features.append({"type": "Feature", "geometry": mapping(LineString([base_pt_vs_t1, t1_pt])), "properties": {"feature_type": "distance_line", "distance_km": round(dist1_m/1000.0, 1), "pair": f"{base_c}-{target_c1}"}})
        features.append({"type": "Feature", "geometry": mapping(LineString([base_pt_vs_t2, t2_pt])), "properties": {"feature_type": "distance_line", "distance_km": round(dist2_m/1000.0, 1), "pair": f"{base_c}-{target_c2}"}})

        geojson_output = {"type": "FeatureCollection", "features": features}
        return jsonify(geojson_output)

    except Exception as e:
        app.logger.error(f"Error preparing GeoJSON data for map: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return jsonify({"error": "Internal Server Error generating map data"}), 500

@app.route('/submit_nickname', methods=['POST'])
def submit_nickname():
    ensure_user_id()
    user_id = session['user_id']

    final_score = session.pop('last_final_score', None)
    data = request.get_json()
    # Only need nickname from client now
    if not data or 'nickname' not in data:
        return jsonify({'error': 'Missing nickname for submission'}), 400

    nickname = data.get('nickname')
    if final_score is None or not isinstance(final_score, int) or final_score < 0:
        app.logger.warning(f"Attempted nickname submission for user {user_id} without a valid final score in session.")
        return jsonify({'error': 'No valid score available for submission.'}), 400

    # Basic validation for nickname
    if not isinstance(nickname, str):
         return jsonify({'error': 'Invalid data type for nickname'}), 400

    nickname = nickname.strip()
    if len(nickname) < 2 or len(nickname) > 20:
        return jsonify({'error': 'Nickname must be between 2 and 20 characters'}), 400
    if not re.match(r'^[\w\s\-\.]+$', nickname):
        return jsonify({'error': 'Nickname contains invalid characters. Allowed: letters, numbers, spaces, underscore, hyphen, period.'}), 400
   
    update_leaderboard(user_id, nickname, final_score)
    top_scores = get_top_scores(LEADERBOARD_SIZE)
    return jsonify({'success': True, 'leaderboard': top_scores})

@app.route('/get_leaderboard', methods=['GET'])
def get_leaderboard_route():
    top_scores = get_top_scores(LEADERBOARD_SIZE)
    return jsonify({'leaderboard': top_scores})
