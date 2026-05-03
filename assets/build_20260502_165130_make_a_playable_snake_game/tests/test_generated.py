import pytest
from flask import Flask, request, jsonify
from unittest.mock import patch, MagicMock

# Import the functions to be tested
from your_module import check_collision, update_game, generate_food  # Replace 'your_module' with the actual module name

# Fixtures to set up the game state
@pytest.fixture
def game_state():
    return {
        'snake': [{'x': 100, 'y': 100}, {'x': 90, 'y': 100}, {'x': 80, 'y': 100}],
        'food': {'x': 200, 'y': 200},
        'direction': 'right',
        'score': 0,
        'game_over': False
    }

# Test check_collision function
def test_check_collision_no_collision(game_state):
    with patch.dict('your_module.game_state', game_state, clear=True):
        check_collision()
        assert game_state['game_over'] is False

def test_check_collision_wall_collision(game_state):
    with patch.dict('your_module.game_state', {'snake': [{'x': 501, 'y': 100}, {'x': 490, 'y': 100}, {'x': 480, 'y': 100}], 'direction': 'right', 'game_over': False}, clear=True):
        check_collision()
        assert game_state['game_over'] is True

def test_check_collision_self_collision(game_state):
    with patch.dict('your_module.game_state', {'snake': [{'x': 100, 'y': 100}, {'x': 100, 'y': 100}, {'x': 90, 'y': 100}], 'direction': 'right', 'game_over': False}, clear=True):
        check_collision()
        assert game_state['game_over'] is True

# Test update_game function
def test_update_game_no_collision_no_food(game_state):
    with patch.dict('your_module.game_state', game_state, clear=True):
        update_game()
        assert game_state['snake'][0]['x'] == 110
        assert game_state['snake'][0]['y'] == 100
        assert game_state['snake'][1]['x'] == 100
        assert game_state['snake'][1]['y'] == 100
        assert game_state['game_over'] is False

def test_update_game_wall_collision(game_state):
    with patch.dict('your_module.game_state', {'snake': [{'x': 501, 'y': 100}, {'x': 490, 'y': 100}, {'x': 480, 'y': 100}], 'direction': 'right', 'game_over': False}, clear=True):
        update_game()
        assert game_state['game_over'] is True

def test_update_game_self_collision(game_state):
    with patch.dict('your_module.game_state', {'snake': [{'x': 100, 'y': 100}, {'x': 100, 'y': 100}, {'x': 90, 'y': 100}], 'direction': 'right', 'game_over': False}, clear=True):
        update_game()
        assert game_state['game_over'] is True

def test_update_game_food_collision(game_state):
    with patch.dict('your_module.game_state', game_state, clear=True), patch('your_module.generate_food', MagicMock):
        game_state['snake'][0]['x'] = 200
        game_state['snake'][0]['y'] = 200
        update_game()
        assert game_state['score'] == 1
        assert game_state['food'] != {'x': 200, 'y': 200}
        assert len(game_state['snake']) == 4

# Test generate_food function
def test_generate_food():
    with patch('random.randint', MagicMock(side_effect=[10, 20])):
        generate_food()
        assert game_state['food'] == {'x': 10, 'y': 20}