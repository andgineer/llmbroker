# llmbroker

Маршрутизатор LLM-запросов к группе бесплатных LLM с автоматическим круговым распределением и паузой при ошибках.

Никаких тяжёлых зависимостей наподобие LangChain и т.п.

## Быстрый старт

Создайте файл `llms.toml` со списком LLM, или скачайте готовый пресет:

```bash
llmbroker preset freetier > llms.toml
```

Пример `llms.toml`:

```toml
[[llms]]
name        = "groq-llama"
base_url    = "https://api.groq.com/openai/v1"
model       = "llama-3.3-70b-versatile"
api_key_ref = "GROQ_API_KEY"

[[llms]]
name        = "groq-gemma"
base_url    = "https://api.groq.com/openai/v1"
model       = "gemma2-9b-it"
api_key_ref = "GROQ_API_KEY"
```

`api_key_ref` — имя переменной окружения с ключом. Секрет никогда не попадает в файл.

Посмотрите, какие ключи нужны пулу, получите их у провайдеров и установите:

```bash
llmbroker env llms.toml > .env
```

Каждый провайдер выдаёт свой ключ (регистрация на бесплатном тарифе занимает около
минуты). `.env`-файл — самый простой вариант, но секреты могут браться и из переменных
окружения, AWS, Vault или любого подключённого бэкенда. Когда ключи установлены,
вызовите брокер:

```python
import llmbroker

llms = llmbroker.Broker("llms.toml")
print(llms.ask("Привет, как дела?").text)
```

Если один endpoint вернул 429, брокер охладит его и переключится на следующий.
Вызывающий код не видит ошибку лимита, пока хотя бы один endpoint доступен.

## Как это работает

- Каждому LLM в пуле соответствует один слот очереди: не более одного активного
  запроса к endpoint-у одновременно.
- На 429/503 брокер ставит endpoint на паузу (`cooldown`) и повторно добавляет его
  в очередь после задержки (`Retry-After` из заголовка или 60 с по умолчанию).
- `ask(prompt)` — обёртка над `chat([{"role": "user", "content": prompt}])`.
- Если все endpoint-ы на паузе и истёк `wait` — `NoLLMAvailableError`.
- Если endpoint попробован и вернул ошибку — `AllLLMsFailedError`.
