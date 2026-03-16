# Demo: Building a Todo App

This shows what happens when you tell the system: **"Build a simple todo app"**

## Input

```
User → Boss: "Build a simple todo app. Alex does backend, Jordan does frontend."
```

## Boss Response (PM Agent)

Boss analyzes the task and delegates — it does NOT write code:

```
@alex: Build a REST API with Python Flask. Endpoints: POST /api/todos (create),
GET /api/todos (list), DELETE /api/todos/:id (delete), PUT /api/todos/:id (toggle).
Use a JSON file for storage. Run on port 5000.

@jordan: Build the frontend with HTML/CSS/JS. Create a single-page UI with:
input field + add button, todo list with checkboxes and delete buttons.
Use fetch() to connect to http://localhost:5000/api/todos.
```

## Alex's Output (Backend)

**`_workspaces/alex/app.py`** — Complete Flask REST API (94 lines):

```python
from flask import Flask, request, jsonify
from flask_cors import CORS
import json, os
from datetime import datetime

app = Flask(__name__)
CORS(app)

@app.route('/api/todos', methods=['GET'])
def get_todos():
    todos = load_todos()
    return jsonify(todos)

@app.route('/api/todos', methods=['POST'])
def create_todo():
    data = request.get_json()
    # ... creates todo with id, title, completed, timestamps
    return jsonify(new_todo), 201

@app.route('/api/todos/<int:todo_id>', methods=['PUT'])
def update_todo(todo_id):
    # ... toggles completion status
    return jsonify(todo)

@app.route('/api/todos/<int:todo_id>', methods=['DELETE'])
def delete_todo(todo_id):
    # ... removes todo
    return jsonify({'message': 'Todo deleted'})

if __name__ == '__main__':
    app.run(debug=True, port=5000)
```

**`_workspaces/alex/requirements.txt`**:
```
Flask==3.0.0
flask-cors==4.0.0
```

## Jordan's Output (Frontend)

**`_workspaces/jordan/_output/todo-frontend/index.html`** — Complete HTML page:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <title>Todo App</title>
</head>
<body>
    <div class="container">
        <h1>📝 Todo List</h1>
        <form id="todo-form">
            <input type="text" id="todo-input" placeholder="What needs to be done?">
            <button type="submit">Add Todo</button>
        </form>
        <div class="filters">
            <button data-filter="all">All</button>
            <button data-filter="pending">Pending</button>
            <button data-filter="completed">Completed</button>
        </div>
        <ul id="todo-list"></ul>
    </div>
    <script src="app.js"></script>
</body>
</html>
```

## Timeline

```
0s    User sends task to Boss
2s    Boss delegates to Alex + Jordan (calls mark_complete)
3s    Alex starts: calls LLM → uses write_file → creates app.py
5s    Jordan starts: calls LLM → uses write_file → creates index.html
~15s  Both agents complete their tasks
```

## What the system did automatically

1. Boss analyzed the task and split it (did not write code)
2. Alex created a full Flask API with CRUD endpoints
3. Jordan created a frontend page with form, filters, and API integration
4. Each agent worked in its own isolated workspace
5. Conversation history was saved to `context.json` for future reference
6. All events were logged to `_logs/` in structured JSONL format
