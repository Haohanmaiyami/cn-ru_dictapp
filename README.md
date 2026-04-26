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


