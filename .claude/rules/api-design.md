---
paths: ["src/knowledge/tools/**"]
---

# API design (el contrato de tools es la API de este servicio)

- `file_path` se devuelve **siempre** sin prefijo y sin corchetes de wikilink.
- Ninguna forma de respuesta cambia sin bump de versión de la tool y sin actualizar el inventario de tools del servidor en el vault.
- Una tool nueva se documenta antes de implementarse.
- Toda tool mutante (`reindex`, `sync_confluence`) exige token de alcance de escritura.
- El servidor nunca escribe en el vault fuera de `90 - Memory/`.
- `include_memories=false` es el default y el cerco se prueba, no se asume.
- Los errores se devuelven con un código estable y un mensaje accionable, nunca con la traza de Python.
- Ningún mensaje de error incluye el bearer, la clave de OpenAI ni rutas absolutas del servidor.
- El frontmatter de todo documento indexado cumple `schema/frontmatter.yml`; un documento inválido se descarta con razón explícita, nunca en silencio.
