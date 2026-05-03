import os
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)

# Game state
game_state = {
    'snake': [{'x': 100, 'y': 100}, {'x': 90, 'y': 100}, {'x': 80, 'y': 100}],
    'food': {'x': 200, 'y': 200},
    'direction': 'right',
    'score': 0,
    'game_over': False
}

# Function to check if the snake is colliding with the walls or itself
def check_collision():
    head = game_state['snake'][0]
    if head['x'] < 0 or head['x'] > 500 or head['y'] < 0 or head['y'] > 500:
        game_state['game_over'] = True
    for segment in game_state['snake'][1:]:
        if head['x'] == segment['x'] and head['y'] == segment['y']:
            game_state['game_over'] = True

# Function to update the game state
def update_game():
    if game_state['game_over']:
        return

    # Move the snake
    new_head = game_state['snake'][0].copy()
    if game_state['direction'] == 'right':
        new_head['x'] += 10
    elif game_state['direction'] == 'left':
        new_head['x'] -= 10
    elif game_state['direction'] == 'up':
        new_head['y'] -= 10
    elif game_state['direction'] == 'down':
        new_head['y'] += 10
    game_state['snake'].insert(0, new_head)

    # Check for collision
    check_collision()

    # Check if the snake eats the food
    if new_head['x'] == game_state['food']['x'] and new_head['y'] == game_state['food']['y']:
        game_state['score'] += 1
        generate_food()
    else:
        game_state['snake'].pop()

# Function to generate food
def generate_food():
    game_state['food'] = {'x': 200, 'y': 200}

# Route to handle game updates
@app.route('/update', methods=['POST'])
def update():
    direction = request.form.get('direction')
    if direction in ['up', 'down', 'left', 'right']:
        game_state['direction'] = direction
    update_game()
    return jsonify(game_state)

# Route to render the game page
@app.route('/')
def index():
    return render_template_string('''
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <title>Snake Game</title>
            <style>
                canvas {
                    border: 1px solid black;
                }
            </style>
        </head>
        <body>
            <canvas id="gameCanvas" width="500" height="500"></canvas>
            <script>
                const canvas = document.getElementById('gameCanvas');
                const ctx = canvas.getContext('2d');
                const game_state = {{ game_state | tojson }};
                const snake = game_state.snake;
                const food = game_state.food;
                const direction = game_state.direction;
                const score = game_state.score;

                function draw() {
                    ctx.clearRect(0, 0, canvas.width, canvas.height);
                    ctx.fillStyle = 'green';
                    snake.forEach(segment => ctx.fillRect(segment.x, segment.y, 10, 10));
                    ctx.fillStyle = 'red';
                    ctx.fillRect(food.x, food.y, 10, 10);
                    ctx.fillStyle = 'black';
                    ctx.font = '16px Arial';
                    ctx.fillText('Score: ' + score, 10, 20);
                }

                function gameLoop() {
                    if (!game_state.game_over) {
                        fetch('/update', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/x-www-form-urlencoded'
                            },
                            body: 'direction=' + direction
                        }).then(response => response.json())
                          .then(data => {
                              game_state = data;
                              draw();
                              setTimeout(gameLoop, 100);
                          });
                    }
                }

                draw();
                gameLoop();
            </script>
        </body>
        </html>
    ''', game_state=game_state)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5600")))