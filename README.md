# <p align="center"><img src="logo.png" alt="ImageGenerate" width="120" /></p> ImageGenerate

Gere imagens com IA sem esforço nem código: digite o prompt + dê contexto e referências se precisar → imagem gerada 🖼️

---

# ❓ Como instalar (do zero até pronto)

1. **Instale o Python 3.10+** (Tkinter já vem na maioria das distros).
2. **Instale a dependência de cofre:** `pip install cryptography` (só se quiser salvar chaves com `remember me`).
3. **Para a versão web:** `pip install -r web/requirements.txt` (FastAPI + uvicorn) e `cd web/frontend && npm install && npm run build`.
4. **Para o launcher Linux:** `bash install.sh` (gera o `.desktop` com o ícone `logo.png`; abra pelo menu, não pelo terminal, para agrupar no dock corretamente).
5. **Para a chave:** crie uma conta no [OpenRouter](https://openrouter.ai), vá em **Keys → Create**, copie o `sk-or-...` (aparece uma vez só) e use `--api-key` ou salve com `--remember-key`. Veja o tutorial completo em [`docs.html`](docs.html) (seção **Conta OpenRouter**).

---

# 3 modos de uso em um só lugar, tudo local 🔒

## 🖥️ Versão GUI (Tkinter)

Abra com `python3 image_generate.py --gui`. Três abas: **Generate** (prompt, proporção, resolução, formato, seed, dry-run, botão Generate/Cancel, cronômetro, log), **Model** (provedor, modelo, temperatura 0–2, chave + remember/Forget) e **Dir** (pastas de saída/contexto/memória + Browse). O `?` no topo abre o `docs.html`. A janela usa `logo.png` como ícone e `WM_CLASS=ImageGenerate` para agrupar no dock do Ubuntu.

<p align="center"><img src="logo-titulo.png" alt="Web" width="520" /></p>

## 🌐 Versão Web (React + FastAPI)

Sirva com `python3 web/server.py` (porta 8000, só `127.0.0.1`). Abra `http://127.0.0.1:8000`. Mesmas 3 abas, cronômetro ao vivo, Cancel, som de alerta no modal de sucesso, folder picker nas pastas locais, busca com highlight no conteúdo, logo como marca d'água no fundo. O `?` no topo abre `/help` (o `docs.html`).

<p align="center"><img src="logo.png" alt="GUI" width="520" /></p>

## 🧑‍💻 Versão CLI (terminal)

Tudo da GUI via flags. Exemplo básico: `python3 image_generate.py --prompt "teste" --prop 1:1 --resolution 512 --dry-run`. Com chave: `export OPENROUTER_API_KEY="sk-or-..."` e depois `--prompt "..." --count 3 --temperature 0.7`. Use `--list-log` para ver o CSV.

---

# 📚 Documentação completa

O arquivo [`docs.html`](docs.html) é a documentação completa do programa — não precisa de internet, abre direto no navegador. Cobre: instalação, CLI, GUI, Web (Generate/Model/Dir + folder picker), pipeline, provedores, cofre de chaves, contexto/memória, resumo automático, cancelamento, log CSV, tabela da API (`/api/generate`, `/api/browse`, etc.), flags, arquivos/pastas e solução de problemas. Use a busca no topo (case-insensitive, com highlight e contador) para encontrar rapidamente o que precisa.

---

# 📝 Direitos reservados

© ImageGenerate — projeto de código aberto. O código (`image_generate.py`, `web/`) está sob a licença do repositório. As imagens geradas pelo usuário pertencem ao usuário (verifique os termos do provedor usado, ex.: OpenRouter). A marca `logo.png` e `logo-titulo.png` são parte deste projeto. Nenhum segredo (chave de API) é armazenado no repositório — apenas via cofre local (`~/.config/image_generate/`) ou flags.
