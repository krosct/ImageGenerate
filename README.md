# 🎨 ImageGenerate

<p align="center"><img src="logo.png" alt="ImageGenerate" width="120" /></p>

Gere imagens por IA sem sair do lugar: prompt + contexto + referências → imagem + log. 🖼️

## ✨ O que faz

- 🖥️ **GUI em 3 abas** — Generate, Model, Dir (só stdlib: Tkinter)
- ⌨️ **CLI completo** — tudo da GUI via terminal
- 🧠 **Contexto** — `.md`/`.txt` entram no prompt automaticamente
- 🖼️ **Memória visual** — imagens de referência guiam a geração
- 📝 **Log** — `log_image_generate.csv` com totais no topo
- 🔑 **Cofre de chaves** — uma chave Fernet por provedor, `remember me`
- ⏱️ **Cronômetro + Cancel** — acompanhe e aborte a geração
- 📝 **Resumo automático** — 1 frase por imagem (modelo à sua escolha)
- 🌐 **Versão web** — mesmo core, interface React (veja abaixo)

## 🚀 Como rodar

```bash
pip install cryptography   # só p/ lembrar chaves
python3 image_generate.py --gui
```

```bash
# CLI
export OPENROUTER_API_KEY="sk-or-..."
python3 image_generate.py --prompt "uma arara voando" --prop 16:9 --resolution 1K
python3 image_generate.py --prompt "teste" --dry-run   # grátis, sem API
python3 image_generate.py --list-log                   # ver histórico
```

<p align="center"><img src="logo-titulo.png" alt="Web" width="520" /></p>

## 🌐 Versão web

```bash
pip install -r web/requirements.txt
cd web/frontend && npm install && npm run build && cd ../..
python3 web/server.py   # abre http://127.0.0.1:8000
```

<p align="center"><img src="logo.png" alt="GUI" width="520" /></p>

Mesmas 3 abas, cronômetro ao vivo, Cancel e log — só local. 🔒

## 🗂️ Onde fica o quê

| 📁 | 📌 |
|---|---|
| `~/Imagens/ImageGenerate/` | imagens + CSV (padrão) |
| `~/.config/image_generate/` | chaves cifradas |

## ⌨️ Flags úteis

`--provider` `--prop` `--resolution` `--context-dir` `--memory-dir` `--remember-key` `--forget-key` `--model` `--seed` `--count` `--temperature`
