import json
import random
import sqlite3
import uuid
import os
import sys
import geopandas as gpd
import re


from flask import Flask, render_template, jsonify, request, session, g
from werkzeug.middleware.proxy_fix import ProxyFix
from shapely.ops import unary_union, nearest_points
from shapely.geometry import LineString, mapping
from pyproj import Geod
from typing import Dict, Optional, List, Set, FrozenSet

# --- Configuration & Constants ---
DATABASE_FILE = 'highscores.db'
LEADERBOARD_SIZE = 5
DISTANCES_DATA_FILE = 'distances.jsonl'
GEOJSON_SHAPES_FILE = 'test.geo.json'
COUNTRY_CODES_FILE = 'country_codes.json'
HIGHSCORE_FILE = 'closer_country_highscore.txt'
SECRET_KEY = 'MC4CAQAwBQYDK2VuBCIEIKN92XnvKX4YfGuwwhJ4XBzqFyQMfXRM7/9KdEMqZcAo'
SHAPES_NAME_COLUMN = 'name_en'
SHAPES_CODE_COLUMN = 'iso_a2'
SHAPES_TYPE_COLUMN = 'type'
VALID_TYPES = ["Country", "Sovereign country"]
GEOMETRY_SIMPLIFY_TOLERANCE = 0.05

app = Flask(__name__)
app.secret_key = SECRET_KEY
# app.logger.setLevel(logging.DEBUG)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

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
    """Initializes the database; creates table if needed."""
    try:
        db = sqlite3.connect(DATABASE_FILE)
        # Check if table exists first to avoid errors on restart
        cursor = db.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='highscores';")
        if not cursor.fetchone():
            app.logger.info("Creating highscores table...")
            cursor.execute(
                "CREATE TABLE highscores (user_id TEXT PRIMARY KEY, highscore INTEGER NOT NULL DEFAULT 0)"
            )
            db.commit()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='leaderboard';")
        if not cursor.fetchone():
             app.logger.info("Creating leaderboard table...")
             # user_id is still PRIMARY KEY to prevent multiple entries per user
             cursor.execute(
                 "CREATE TABLE leaderboard (user_id TEXT PRIMARY KEY, nickname TEXT, score INTEGER NOT NULL DEFAULT 0)"
             )
             db.commit()
             app.logger.info("Leaderboard table created.")

        db.close()
    except Exception as e:
         app.logger.error(f"Failed to initialize database: {e}", exc_info=True)

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

    if 'score' not in session:
         session['score'] = 0
         app.logger.debug(f"GET /start_round: Score missing, reset to 0 for user {user_id}")

    global country_list, country_code_map
    if not country_list or not country_code_map: return jsonify({'error': 'Game data not fully loaded'}), 500
    valid_start_countries = [c for c in country_list if c in country_code_map]
    if not valid_start_countries: return jsonify({'error': 'No countries with codes found'}), 500
    if 'score' not in session or not session.get('base_country') or session['base_country'] not in country_code_map:
        session['score'] = 0
        session['base_country'] = random.choice(valid_start_countries)

    base_country_name = session['base_country']
    score = session['score']
    session['user_highscore'] = user_highscore
    session.modified = True 

    possible_targets = [c for c in country_list if c != base_country_name and c in country_code_map]
    if len(possible_targets) < 2: return jsonify({'game_over': True, 'message': 'Not enough valid countries with flags!'}), 200
    target1_name, target2_name = random.sample(possible_targets, 2)
    target1_code, target2_code, base_code = country_code_map.get(target1_name), country_code_map.get(target2_name), country_code_map.get(base_country_name)
    if not target1_code or not target2_code or not base_code: return jsonify({'error': 'Internal data inconsistency (codes)'}), 500
    return jsonify({
        'base_country': {'name': base_country_name, 'code': base_code },
        'target1': {'name': target1_name, 'code': target1_code},
        'target2': {'name': target2_name, 'code': target2_code},
        'score': score,
        'highscore': user_highscore,
    })

@app.route('/make_guess', methods=['POST'])
def make_guess():
    ensure_user_id()
    user_id = session['user_id']
    current_user_highscore = get_user_highscore(user_id)
    session['user_highscore'] = current_user_highscore

    global distances_data
    if distances_data is None: return jsonify({'error': 'Distance data not loaded'}), 500
    data = request.get_json()
    if not data or 'base_country_name' not in data or 'chosen_country_name' not in data or 'other_country_name' not in data: return jsonify({'error': 'Missing name data in guess request'}), 400
    base_c, chosen_c, other_c = data['base_country_name'], data['chosen_country_name'], data['other_country_name']
    dist_chosen, dist_other = get_distance(base_c, chosen_c), get_distance(base_c, other_c)
    if dist_chosen is None or dist_other is None: return jsonify({'error': f'Internal error: Missing distance data for {base_c}.'}), 500
    is_correct = (dist_chosen <= dist_other)
    closer_country = chosen_c if is_correct else other_c
    response_data = {'correct': is_correct, 'chosen_dist': round(dist_chosen, 1), 'other_dist': round(dist_other, 1), 'closer_country': closer_country}
    current_score = session.get('score', 0) 
    if is_correct:
        current_score += 1
        session['score'] = current_score
        session['base_country'] = chosen_c
        response_data['score'] = current_score

        if current_score > current_user_highscore:
            app.logger.info(f"New high score for user {user_id}: {current_score}")
            update_user_highscore(user_id, current_score)
            session['user_highscore'] = current_score
            response_data['new_highscore'] = True

        response_data['highscore'] = session['user_highscore']
        session.modified = True
    else:
        final_score = current_score
        response_data['game_over'] = True
        response_data['final_score'] = final_score
        response_data['highscore'] = current_user_highscore
        response_data['map_available'] = True
        response_data['map_params'] = {'base': base_c, 't1': chosen_c, 't2': other_c}

        # Check if final score is a new high score for this user
        if final_score > 0 and final_score >= current_user_highscore:
             # >= ensures if they match their high score, they can still enter name
            response_data['prompt_nickname'] = True
            existing_nickname = get_user_nickname(user_id)
            response_data['existing_nickname'] = existing_nickname 
            app.logger.debug(f"Game over for user {user_id}. Final score {final_score} is a new/matching high score. Prompting for nickname.")
        else:
             response_data['prompt_nickname'] = False
             app.logger.debug(f"Game over for user {user_id}. Final score {final_score} is not a new high score ({current_user_highscore}).")

        session.pop('score', None) # Remove score to trigger reset in /start_round
        session.pop('base_country', None)
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

    update_leaderboard(user_id, nickname, final_score)
    top_scores = get_top_scores(LEADERBOARD_SIZE)
    return jsonify({'success': True, 'leaderboard': top_scores})

@app.route('/get_leaderboard', methods=['GET'])
def get_leaderboard_route():
     top_scores = get_top_scores(LEADERBOARD_SIZE)
     return jsonify({'leaderboard': top_scores})
