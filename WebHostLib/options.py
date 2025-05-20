import collections.abc
import json
import os
from textwrap import dedent
from typing import Dict, Union
from docutils.core import publish_parts

import yaml
from flask import redirect, render_template, request, Response, abort

import Options
from Utils import local_path
from worlds.AutoWorld import AutoWorldRegister
from . import app, cache
from .generate import get_meta


def create() -> None:
    target_folder = local_path("WebHostLib", "static", "generated")
    yaml_folder = os.path.join(target_folder, "configs")

    Options.generate_yaml_templates(yaml_folder)


def get_world_theme(game_name: str) -> str:
    if game_name in AutoWorldRegister.world_types:
        return AutoWorldRegister.world_types[game_name].web.theme
    return 'grass'


def render_options_page(template: str, world_name: str, is_complex: bool = False) -> Union[Response, str]:
    world = AutoWorldRegister.world_types[world_name]
    if world.hidden or world.web.options_page is False:
        return redirect("games")
    visibility_flag = Options.Visibility.complex_ui if is_complex else Options.Visibility.simple_ui

    start_collapsed = {"Game Options": False}
    for group in world.web.option_groups:
        start_collapsed[group.name] = group.start_collapsed

    return render_template(
        template,
        world_name=world_name,
        world=world,
        option_groups=Options.get_option_groups(world, visibility_level=visibility_flag),
        start_collapsed=start_collapsed,
        issubclass=issubclass,
        Options=Options,
        theme=get_world_theme(world_name),
    )


def generate_game(options: Dict[str, Union[dict, str]]) -> Union[Response, str]:
    from .generate import start_generation
    return start_generation(options, get_meta({}))


def send_yaml(player_name: str, formatted_options: dict) -> Response:
    response = Response(yaml.dump(formatted_options, sort_keys=False))
    response.headers["Content-Type"] = "text/yaml"
    response.headers["Content-Disposition"] = f"attachment; filename={player_name}.yaml"
    return response


@app.template_filter("dedent")
def filter_dedent(text: str) -> str:
    return dedent(text).strip("\n ")


@app.template_filter("rst_to_html")
def filter_rst_to_html(text: str) -> str:
    """Converts reStructuredText (such as a Python docstring) to HTML."""
    if text.startswith(" ") or text.startswith("\t"):
        text = dedent(text)
    elif "\n" in text:
        lines = text.splitlines()
        text = lines[0] + "\n" + dedent("\n".join(lines[1:]))

    return publish_parts(text, writer_name='html', settings=None, settings_overrides={
        'raw_enable': False,
        'file_insertion_enabled': False,
        'output_encoding': 'unicode'
    })['body']


@app.template_test("ordered")
def test_ordered(obj):
    return isinstance(obj, collections.abc.Sequence)


@app.route("/games/<string:game>/option-presets", methods=["GET"])
@cache.cached()
def option_presets(game: str) -> Response:
    world = AutoWorldRegister.world_types[game]

    presets = {}
    for preset_name, preset in world.web.options_presets.items():
        presets[preset_name] = {}
        for preset_option_name, preset_option in preset.items():
            if preset_option == "random":
                presets[preset_name][preset_option_name] = preset_option
                continue

            option = world.options_dataclass.type_hints[preset_option_name].from_any(preset_option)
            if isinstance(option, Options.NamedRange) and isinstance(preset_option, str):
                assert preset_option in option.special_range_names, \
                    f"Invalid preset value '{preset_option}' for '{preset_option_name}' in '{preset_name}'. " \
                    f"Expected {option.special_range_names.keys()} or {option.range_start}-{option.range_end}."

                presets[preset_name][preset_option_name] = option.value
            elif isinstance(option, (Options.Range, Options.OptionSet, Options.OptionList, Options.OptionCounter)):
                presets[preset_name][preset_option_name] = option.value
            elif isinstance(preset_option, str):
                # Ensure the option value is valid for Choice and Toggle options
                assert option.name_lookup[option.value] == preset_option, \
                    f"Invalid option value '{preset_option}' for '{preset_option_name}' in preset '{preset_name}'. " \
                    f"Values must not be resolved to a different option via option.from_text (or an alias)."
                # Use the name of the option
                presets[preset_name][preset_option_name] = option.current_key
            else:
                # Use the name of the option
                presets[preset_name][preset_option_name] = option.current_key

    class SetEncoder(json.JSONEncoder):
        def default(self, obj):
            from collections.abc import Set
            if isinstance(obj, Set):
                return list(obj)
            return json.JSONEncoder.default(self, obj)

    json_data = json.dumps(presets, cls=SetEncoder)
    response = Response(json_data)
    response.headers["Content-Type"] = "application/json"
    return response


@app.route("/weighted-options")
def weighted_options_old():
    return redirect("games", 301)


@app.route("/games/<string:game>/weighted-options")
@cache.cached()
def weighted_options(game: str):
    try:
        return render_options_page("weightedOptions/weightedOptions.html", game, is_complex=True)
    except KeyError:
        return abort(404)


@app.route("/games/<string:game>/generate-weighted-yaml", methods=["POST"])
def generate_weighted_yaml(game: str):
    if request.method == "POST":
        intent_generate = False
        options = {}

        for key, val in request.form.items():
            if "||" not in key:
                if len(str(val)) == 0:
                    continue

                options[key] = val
            else:
                if int(val) == 0:
                    continue

                [option, setting] = key.split("||")
                options.setdefault(option, {})[setting] = int(val)

        # Error checking
        if "name" not in options:
            return "Player name is required."

        # Remove POST data irrelevant to YAML
        if "intent-generate" in options:
            intent_generate = True
            del options["intent-generate"]
        if "intent-export" in options:
            del options["intent-export"]

        # Properly format YAML output
        player_name = options["name"]
        del options["name"]

        formatted_options = {
            "name": player_name,
            "game": game,
            "description": f"Generated by https://archipelago.gg/ for {game}",
            game: options,
        }

        if intent_generate:
            return generate_game({player_name: formatted_options})

        else:
            return send_yaml(player_name, formatted_options)


# Player options pages
@app.route("/games/<string:game>/player-options")
@cache.cached()
def player_options(game: str):
    try:
        return render_options_page("playerOptions/playerOptions.html", game, is_complex=False)
    except KeyError:
        return abort(404)


# YAML generator for player-options
@app.route("/games/<string:game>/generate-yaml", methods=["POST"])
def generate_yaml(game: str):
    if request.method == "POST":
        options = {}
        intent_generate = False
        for key, val in request.form.items(multi=True):
            if key in options:
                if not isinstance(options[key], list):
                    options[key] = [options[key]]
                options[key].append(val)
            else:
                options[key] = val

        for key, val in options.copy().items():
            key_parts = key.rsplit("||", 2)
            # Detect and build OptionCounter options from their name pattern
            if key_parts[-1] == "qty":
                if key_parts[0] not in options:
                    options[key_parts[0]] = {}
                if val != "0":
                    options[key_parts[0]][key_parts[1]] = int(val)
                del options[key]

            # Detect keys which end with -custom, indicating a TextChoice with a possible custom value
            elif key_parts[-1].endswith("-custom"):
                if val:
                    options[key_parts[-1][:-7]] = val

                del options[key]

            # Detect keys which end with -range, indicating a NamedRange with a possible custom value
            elif key_parts[-1].endswith("-range"):
                if options[key_parts[-1][:-6]] == "custom":
                    options[key_parts[-1][:-6]] = val

                del options[key]

        # Detect random-* keys and set their options accordingly
        for key, val in options.copy().items():
            if key.startswith("random-"):
                options[key.removeprefix("random-")] = "random"
                del options[key]

        # Error checking
        if not options["name"]:
            return "Player name is required."

        # Remove POST data irrelevant to YAML
        preset_name = 'default'
        if "intent-generate" in options:
            intent_generate = True
            del options["intent-generate"]
        if "intent-export" in options:
            del options["intent-export"]
        if "game-options-preset" in options:
            preset_name = options["game-options-preset"]
            del options["game-options-preset"]

        # Properly format YAML output
        player_name = options["name"]
        del options["name"]

        description = f"Generated by https://archipelago.gg/ for {game}"
        if preset_name != 'default' and preset_name != 'custom':
            description += f" using {preset_name} preset"

        formatted_options = {
            "name": player_name,
            "game": game,
            "description": description,
            game: options,
        }

        if intent_generate:
            return generate_game({player_name: formatted_options})

        else:
            return send_yaml(player_name, formatted_options)


# Game options landing page
@app.route("/game-options")
@cache.cached()
def game_options():
    available_games = []
    custom_worlds_map = {}  # Initialize outside the if block
    custom_worlds_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'custom_worlds')
    
    # Get list of custom worlds
    if os.path.exists(custom_worlds_dir):
        print(f"Custom worlds directory exists at: {custom_worlds_dir}")
        print(f"Contents of directory: {os.listdir(custom_worlds_dir)}")
        # Create a mapping of lowercase game names to their .apworld files
        for f in os.listdir(custom_worlds_dir):
            if f.endswith('.apworld'):
                # Convert filename to a comparable format (lowercase, no underscores)
                base_name = f.replace('.apworld', '').replace('_', ' ').lower()
                custom_worlds_map[base_name] = f.replace('.apworld', '')
        print(f"Found custom worlds map: {custom_worlds_map}")
    else:
        print(f"Custom worlds directory does not exist at: {custom_worlds_dir}")
    
    # Specific mappings for games that have .apworld files but different names
    game_name_mappings = {
        'another crabs treasure': 'another_crab',
        'ape escape': 'apeescape',
        'archipela-go!': 'apgo',
        'an untitled story': 'aus',
        'banjo-tooie': 'banjo_tooie',
        'candy box 2': 'candybox2',
        'celeste (open world)': 'celeste_open_world',
        'donkey kong country 2': 'dkc2',
        'dark souls remastered': 'dsr',
        'ender lilies': 'enderlilies',
        'final fantasy 12 open world': 'ff12_open_world',
        'final fantasy iv free enterprise': 'ff4fe',
        'final fantasy tactics advance': 'ffta',
        'final fantasy tactics a2': 'ffta2',
        'final fantasy xii trial mode': 'ffxiitm',
        'final fantasy xiv': 'ffxiv',
        'yu-gi-oh! forbidden memories': 'fm',
        'golden sun the lost age': 'gstla',
        'here comes niko!': 'hcniko',
        'jak and daxter the precursor legacy': 'jakanddaxter',
        'keymaster\'s keep': 'keymasters_keep',
        'kingdom hearts birth by sleep': 'khbbs',
        'kingdom hearts chain of memories': 'khcom',
        'kingdom hearts re chain of memories': 'khrecom',
        'kirby super star': 'kss',
        'keep talking and nobody explodes': 'ktane',
        'links awakening dx beta': 'ladx_beta',
        'super mario land 2': 'marioland2',
        'hatsune miku project diva mega mix+': 'megamix',
        'metroid prime': 'metroidprime',
        'mega man 3': 'mm3',
        'mega man x': 'mmx',
        'mega man x2': 'mmx2',
        'mega man x3': 'mmx3',
        'metroid zero mission': 'mzm',
        'scooby-doo! night of 100 frights': 'no100f',
        'ori and the blind forest': 'oribf',
        'osu!': 'osu',
        'paper mario': 'papermario',
        'pokemon mystery dungeon explorers of sky': 'pmd_eos',
        'pokemon firered and leafgreen': 'pokemon_frlg',
        'ratchet & clank 2': 'rac2',
        'resident evil 2 remake': 'residentevil2remake',
        'risk of rain': 'ror1',
        'sonic adventure dx': 'sadx',
        'simon tatham\'s portable puzzle collection': 'sgtpuzzles',
        'the simpsons hit and run': 'simpsonshitnrun',
        'the sims 4': 'sims4',
        'sly cooper and the thievius raccoonus': 'sly1',
        'sonic riders': 'sonicriders',
        'sonic heroes': 'sonic_heroes',
        'castlevania - circle of the moon': 'sotn',
        'soul blazer': 'soulblazer',
        'spelunky 2': 'spelunky2',
        'star fox 64': 'star_fox_64',
        'star wars episode i racer': 'swr',
        'the binding of isaac repentance': 'tboir',
        'toejam and earl': 'tje',
        'the legend of zelda - oracle of ages': 'tloz_ooa',
        'the legend of zelda - oracle of seasons': 'tloz_oos',
        'the minish cap': 'tmc',
        'twilight princess': 'Twilight Princess',
        'void stranger': 'voidstranger',
        'wario land': 'wl',
        'wario land 4': 'wl4',
        'xcom 2 war of the chosen': 'x2wotc',
        'yoku\'s island express': 'yoku',
        'zelda ii: the adventure of link': 'zelda2'
    }
    
    # Track unmatched games for debugging
    unmatched_games = []
    matched_games = []
    
    for game in AutoWorldRegister.world_types:
        if not AutoWorldRegister.world_types[game].hidden:  # Only exclude hidden games
            has_settings = bool(AutoWorldRegister.world_types[game].web.options_page)
            # Check if this game has a matching .apworld file
            game_key = game.lower().replace('_', ' ')
            
            # Try to find a match using the game name mappings
            apworld_name = None
            if game_key in game_name_mappings:
                apworld_name = game_name_mappings[game_key]
            elif game_key in custom_worlds_map:
                apworld_name = custom_worlds_map[game_key]
            
            # Debug logging
            if apworld_name:
                matched_games.append((game, game_key))
            else:
                unmatched_games.append((game, game_key))
            
            available_games.append({ 
                'title': game, 
                'has_settings': has_settings,
                'apworld_name': apworld_name
            })
    
    print("\nMatched games:")
    for game, key in matched_games:
        print(f"  {game} -> {key}")
    
    print("\nUnmatched games:")
    for game, key in unmatched_games:
        print(f"  {game} -> {key}")
    
    print(f"\nTotal games: {len(available_games)}")
    print(f"Games with download buttons: {len([g for g in available_games if g['apworld_name'] is not None])}")
    
    return render_template("gameOptions.html", games=available_games, custom_worlds=custom_worlds_map)
