# Monitor das linhas 0.167 e 167.1

O programa consulta as camadas públicas da Semob/DF e envia alertas pelo Telegram
quando um veículo das linhas **0.167** ou **167.1** estiver, aproximadamente, a
**30 ou 15 minutos** da parada **L2 Sul | SAUS (OAB / Colégio Galois)**.

## Antes de subir para o GitHub

O `config.json` contém um intervalo inicial de **16h às 21h, de segunda a sexta**.
Altere `monitor_start` e `monitor_end` conforme o horário real em que vocês usam o ônibus.

As coordenadas do arquivo são apenas um ponto aproximado. No primeiro teste, execute:

```powershell
python monitor_onibus.py --discover
```

Confira se o terminal exibe a parada OAB/Galois. Caso escolha outra parada, substitua
`approximate_lat` e `approximate_lon` pelas coordenadas copiadas do Google Maps.

## Teste no Windows

1. Instale o Python 3.12.
2. Abra o PowerShell dentro da pasta do projeto.
3. Crie e ative o ambiente:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

4. Defina temporariamente os dados do Telegram:

```powershell
$env:TELEGRAM_BOT_TOKEN="COLE_AQUI_O_TOKEN"
$env:TELEGRAM_CHAT_IDS="SEU_CHAT_ID,CHAT_ID_DA_AMIGA"
```

5. Teste o envio:

```powershell
python monitor_onibus.py --test-telegram
```

6. Confira a parada e os campos retornados pela Semob:

```powershell
python monitor_onibus.py --discover
```

7. Faça uma consulta sem aguardar o intervalo:

```powershell
python monitor_onibus.py --once
```

8. Inicie o monitor normal:

```powershell
python monitor_onibus.py
```

Use `Ctrl+C` para interromper.

## Como descobrir o chat_id da amiga sem usar site de terceiros

1. Envie a ela o link do seu bot: `https://t.me/NOME_DO_SEU_BOT`.
2. Ela deve abrir o link, tocar em **Iniciar** e enviar uma mensagem identificável,
   por exemplo: `BRUNA ONIBUS 167`.
3. No seu computador, mantenha apenas o token definido e execute:

```powershell
$env:TELEGRAM_BOT_TOKEN="COLE_AQUI_O_TOKEN"
python monitor_onibus.py --show-updates
```

4. O terminal mostrará o nome, o usuário e o `chat_id` correspondente.
5. A amiga pode lhe mandar apenas o número do `chat_id`. Ela **não precisa e não deve**
   receber o token do bot.

Caso não apareça nenhuma conversa, ela deve enviar outra mensagem ao bot e você executa
o comando novamente.

## Configuração no GitHub

No repositório, abra:

**Settings → Secrets and variables → Actions → New repository secret**

Crie:

- `TELEGRAM_BOT_TOKEN`: token fornecido pelo BotFather;
- `TELEGRAM_CHAT_IDS`: os dois números separados por vírgula, por exemplo
  `123456789,987654321`.

Nunca coloque o token ou os chat_ids diretamente no código.

Em seguida, abra **Actions → Monitor de ônibus DF → Run workflow** para fazer um teste
manual. O arquivo `.github/workflows/monitor.yml` também inicia o monitor às 18:50 UTC,
de segunda a sexta. Ajuste o `cron` caso altere significativamente o intervalo.

## Como funciona a estimativa

O programa:

- identifica a parada por palavras-chave e proximidade;
- localiza os veículos das duas linhas;
- projeta ônibus e parada no itinerário publicado;
- confirma que o veículo está se aproximando em duas consultas consecutivas;
- estima o tempo com a distância pelo trajeto, a velocidade observada e um fator de trânsito;
- evita repetir o mesmo alerta para o mesmo veículo.

A previsão não é garantia de chegada. Trânsito, desvios, ausência de atualização do GPS e
mudanças no formato do serviço da Semob podem afetar o resultado.

## Comandos úteis

```text
python monitor_onibus.py --show-updates
python monitor_onibus.py --test-telegram
python monitor_onibus.py --discover
python monitor_onibus.py --once
python monitor_onibus.py
```
