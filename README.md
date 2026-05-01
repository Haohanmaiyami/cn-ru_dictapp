# 🇨🇳 Chinese–Russian Dictionary App

A modern Chinese–Russian dictionary and language tool built with FastAPI + SwiftUI.

This project replaces outdated dictionary apps by combining:
- a large-scale dictionary database
- AI-powered translation and analysis
- native stroke order rendering for Chinese characters

---

## 🚀 Features

### 🔍 Smart Dictionary Search
- Fast Chinese → Russian lookup
- Cleaned and deduplicated results
- Smart ranking (exact match > prefix > contains)
- Pinyin normalization (ni hao = nǐ hǎo)

---

### 🤖 AI Translation & Analysis
- Chinese → Russian explanation
- Russian → Chinese translation
- Structured output (meanings, grammar, examples)
- Local LLM via Ollama

---

### 🧠 Dictionary-Aware AI
- AI uses dictionary hits inside responses
- Extracts key words from text
- Improves translation accuracy

---

### ✍️ Stroke Order Rendering
- Native character stroke animation
- Built with hanzi-writer
- No external redirects

---

### 📱 iOS App (SwiftUI)
- Clean modern UI
- Dark / Light mode
- Smooth navigation

---

## 🏗 Tech Stack

Backend:
- FastAPI
- PostgreSQL
- SQLAlchemy (async)
- Alembic
- Ollama (LLM)

Frontend:
- SwiftUI
- WKWebView (stroke order)

---

## 📂 Project Structure

backend/
  dictapp/
    api/
    repo/
    models/
    schemas/
    services/
  alembic/
  main.py

ios-app/
  Models/
  Network/
  Views/
  Components/

---

## ⚙️ Setup

1. Clone repo
git clone https://github.com/YOUR_USERNAME/cn-ru_dictapp.git
cd cn-ru_dictapp

2. Backend
poetry install
cp .env.example .env
uvicorn main:app --reload

3. Run AI (Ollama)
ollama run qwen2.5:7b

4. iOS App
- Open in Xcode
- Run on simulator or device
- Update API base URL if needed

---

## 🔌 API Endpoints

/api/search — Dictionary search  
/api/ai/analyze — CN → RU analysis  
/api/ai/translate-ru-to-cn — RU → CN translation  

---

## 🎯 Goals

- Build the best Chinese–Russian dictionary app
- Combine dictionary + AI
- Provide a professional language tool

---

## 👤 Author

Ayan Kharitonov  
Chinese language specialist & backend developer  

---

## ⭐ Future Plans

- Flutter app
- User accounts
- Favorites & history
- Cloud deployment
- AI scaling (queue + workers)

---

## 📸 Screenshots

(add later)

---

## 📄 License

MIT













name dictionaryuser
password kaishi123 password in docker same
database dictionarydb
owner dictionaryuser


-----
migrations for future
## Migrations (Alembic)
Create migration:
```

poetry run alembic revision --autogenerate -m "..."
```
Apply:
```

poetry run alembic upgrade head
```

back to normal


