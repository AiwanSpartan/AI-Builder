# 🏢 AI Office Builder

A multi-agent Python backend that automates the software development lifecycle. It uses a "team" of specialized AI agents to plan, code, debug, and test applications in real-time.

## 🚀 What it does

Imagine if you had a tiny software company living inside your computer. You give them a prompt, and they work together to hand you a finished `.py` file.

* **Architect**: Plans the structure.
* **Coder**: Writes the implementation.
* **Debugger**: Runs the code and fixes errors automatically (up to 5 attempts).
* **Tester**: Writes `pytest` units for the logic.
* **Reviewer**: Gives a final quality score and suggestions.

## 🛠️ Tech Stack

* **Backend**: Flask (Python)
* **Communication**: WebSockets (Live updates) & Server-Sent Events (Process streaming)
* **AI Engine**: Ollama (Running local models like `DeepSeek-R1`, `Qwen2.5-Coder`, and `Mistral`)

## 📋 Requirements

1. **Ollama** installed and running locally.
2. The following models pulled:
```bash
ollama pull deepseek-r1:7b
ollama pull qwen2.5-coder:7b
ollama pull deepseek-coder:6.7b
ollama pull mistral:7b

```



## 🏃 Quick Start

1. **Clone the repo**
2. **Install dependencies**:
```bash
pip install flask flask-cors flask-sock ollama

```


3. **Configure Paths**: Update `BASE_DIR` in `app.py` to your local folder.
4. **Run the Server**:
```bash
python app.py

```


5. **Build**: Send a POST request to `/build` with your app idea!

## 📂 Project Structure

* `/Builds`: Stores every version of the apps created.
* `built_app.py`: The most recent, successful build ready to run.
* `office.html`: The frontend dashboard (accessible at `http://localhost:5000/office`).
