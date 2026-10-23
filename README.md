# contraglosa-validacao-live

Aplicação de validação de mercado para clínicas médicas, com formulário
click-only, bloqueio de respostas duplicadas e painel administrativo protegido.

## Executar

Não há dependências externas. No Codespaces:

```bash
python3 app_codespaces.py
```

O servidor escuta em `0.0.0.0:8080`. Para evitar o erro `Address already in
use`, pare uma instância anterior com `Ctrl+C` antes de iniciar outra.

## Links

Com a porta 8080 pública no Codespaces:

- **Formulário para respondentes:**  
  https://humble-cod-p5479q7q66j266jr-8080.app.github.dev/entrevista
- **Área administrativa:**  
  https://humble-cod-p5479q7q66j266jr-8080.app.github.dev/admin

O endereço pode mudar quando o Codespace for recriado. Acesse a aba
**Ports**, torne a porta `8080` pública e copie o endereço exibido.

## Funcionamento

- `/entrevista` exibe as nove perguntas de múltipla escolha e os campos Nome,
  CRM e plano de saúde.
- Ao escolher **Outro plano**, aparece um campo para informar o nome do plano.
- O servidor cria um cookie `HttpOnly` persistente por dispositivo e calcula
  seu hash para impedir uma segunda resposta no mesmo aparelho.
- Uma duplicidade é rejeitada no servidor com HTTP `409`.
- Cada resposta é salva em `dados/respostas_clinicas.jsonl`.
- O roteiro do pitch é recalculado em `docs/ROTEIRO-ENTREVISTA.md`.
- A fila de 10 slots é salva em `dados/slots_clinicas.json`.
- O painel `/admin` exige a senha definida em `LEADER_PASSWORD`, no início de
  `app_codespaces.py`. Após o login, ele mostra os KPIs e cada respondente
  individualmente, além de permitir copiar o relatório Markdown.

## Segurança e dados

O formulário é público, mas as respostas e a API do dashboard exigem sessão
administrativa. O cookie impede duplicidade por dispositivo, mas não substitui
uma identidade forte: apagar cookies ou usar outro dispositivo pode permitir
novo envio. O arquivo `.leader_token` e os arquivos de respostas ficam em
`dados/` e não devem ser publicados no Git.

O Codespaces precisa permanecer ativo para o site continuar acessível. Para
hospedagem permanente, use um serviço que execute Python e ofereça
armazenamento persistente; GitHub Pages (`github.io`) não executa este servidor
nem grava respostas.

## Deploy no Render e exportação Markdown

O `render.yaml` configura o Web Service gratuito, usa a variável `PORT` do
Render e monitora `/healthz`. O servidor gera automaticamente o consolidado
`docs/ROTEIRO-ENTREVISTA.md` e um arquivo por entrevista em
`docs/pesquisas/entrevista_XX_persona.md`.

No painel autenticado, os botões de download usam `/api/exportar-mds`. Essa
rota exige a sessão do líder para não expor nome, CRM e plano de saúde. Para
extrair de um Render autenticado, passe o valor do cookie `leader_session`
como segundo argumento:

```bash
python3 scripts/extrair_para_mds.py https://seu-app.onrender.com SEU_LEADER_SESSION
```

Sem URL, o script lê `dados/respostas_clinicas.jsonl` localmente:

```bash
python3 scripts/extrair_para_mds.py
```
