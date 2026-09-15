# <p align="center"><img src="img/logo.png" alt="ImageGenerate" width="120" /></p> ImageGenerate

Gere imagens com IA sem esforço nem código: digite o prompt + dê contexto e referências se precisar → imagem gerada 🖼️


# 🎯 Funcionalidade

**Como usar (resumo):**

1. **Escolha a interface:** CLI (`python3 image_generate.py --gui` para desktop, `python3 web/server.py` para navegador, ou apenas flags no terminal).
2. **Defina o prompt:** digite o que quer ver (ex.: `"um farol à noite"`). Adicione contexto (`--context-dir`) ou referências visuais (`--memory-dir`) se precisar de consistência.
3. **Ajuste a saída:** escolha proporção (`--prop`), resolução (`--resolution`), formato (`--output-format`) e, se quiser, temperatura (`--temperature 0.7`) e quantidade (`--count 3`).
4. **Gere:** clique em **Generate** (GUI/Web) ou rode o comando (CLI). Se `count > 1`, confirma o custo adicional.
5. **Veja o resultado:** a imagem aparece no modal (Web) ou no diálogo (GUI); o caminho está no log (`log_image_generate.csv`) e pode ser copiado.
6. **Documentação completa:** abra [`docs.html`](docs.html) para instalação detalhada, tutorial OpenRouter, tabela de flags, API do backend e solução de problemas.

---

# ❓ Como instalar (do zero até pronto)

1. **Instale o Python 3.10+**:
```bash
sudo apt update
sudo apt install python3.11 -y
```

2. **Instale a dependência de cofre**:
```bash
pip install cryptography
```

3. **Para a versão web**:
```bash
pip install -r web/requirements.txt`
cd web/frontend
npm install
npm run build
```

4. **Para o launcher Linux**:
Gera o `.desktop`; podendo ser aberto pelo menu.
```bash
bash install.sh
```

---

# 🔒 3 modos de uso em um só lugar, tudo local

## 🖥️ Versão GUI (Tkinter)

Rode com:
```bash
python3 image_generate.py --gui
```
Ou iniciando **image-generate.desktop** (launcher gerado pelo install.sh).

<p align="center"><img src="img/guitk.png" alt="GUI" width="520" /></p>

## 🌐 Versão web

Sirva com com `server.py` e acesse em `http://127.0.0.1:8000`.

```bash
python3 web/server.py   # abre http://127.0.0.1:8000
```

<p align="center"><img src="img/guiweb.png" alt="Web" width="520" /></p>

## 🧑‍💻 Versão CLI (terminal)

Exemplos:
```bash
# Básico (sem chave, só dry-run)
python3 image_generate.py --prompt "teste" --prop 1:1 --resolution 512 --dry-run

# Com chave
export OPENROUTER_API_KEY="sk-or-..."
python3 image_generate.py --prompt "uma arara voando" --prop 16:9 --resolution 1K --count 3 --temperature 0.7

# Ver histórico
python3 image_generate.py --list-log
```

---

# 📚 Documentação completa

O arquivo [`docs.html`](docs.html) é a documentação completa do programa — não precisa de internet, abre direto no navegador. Use a busca no topo para encontrar rapidamente o que precisa.
