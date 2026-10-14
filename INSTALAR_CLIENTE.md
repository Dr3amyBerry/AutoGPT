# RigorCore Dev: cliente Windows 0.1.1

Cliente con el mismo menu de bandeja, selector de carpeta y lanzadores de la
instalacion original. Requiere Windows x64, Python 3.11 o superior e Internet
para descargar las dependencias. No incluye Python ni credenciales.

## Instalacion

Extrae el ZIP en una carpeta permanente. Abre PowerShell dentro de ella:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[windows]"
```

Comprueba que cada comando termina correctamente antes de continuar.

## Configurar esta PC

Elige una carpeta existente para exponer al servidor. Cada PC necesita un
identificador unico: no uses `windows-pc`, que pertenece al equipo original.

```powershell
$workspacePath = Read-Host 'Ruta completa de la carpeta de trabajo'
$nodeId = 'pc-' + $env:COMPUTERNAME
.\.venv\Scripts\python.exe -m rigorcore_dev.cli init --server wss://mcp.rigorcore.com/agents/ws --node-id $nodeId --name $env:COMPUTERNAME --workspace "app=$workspacePath" --mode auto
```

Si otro equipo tiene el mismo nombre, cambia `$nodeId` por un identificador
distinto usando letras, numeros, guiones o guiones bajos. `init` no sobrescribe
una configuracion existente en `%USERPROFILE%\.config\rigorcore-dev\agent.json`.

Configura el token del agente suministrado por el propietario del servidor.
Se solicita oculto y no se imprime ni se incluye en el ZIP:

```powershell
$agentSecret = Read-Host 'Token del agente' -AsSecureString
$env:RIGORCORE_AGENT_TOKEN = [System.Net.NetworkCredential]::new('', $agentSecret).Password
[Environment]::SetEnvironmentVariable('RIGORCORE_AGENT_TOKEN', $env:RIGORCORE_AGENT_TOKEN, 'User')
Remove-Variable agentSecret
```

Esta instancia del servidor autoriza a un solo propietario. Esta instalacion
agrega otra PC de ese propietario; no crea una cuenta independiente.

## Iniciar y verificar

Abre `Iniciar_RigorCore.bat`. Haz clic derecho en el icono de bandeja y comprueba
que aparezca **Connected** y la carpeta elegida. La configuracion anterior usa
**Full Auto**, igual que la PC original: los comandos se ejecutan sin aprobacion
individual. Puedes cambiarlo desde el menu a **Approve each shell command**.

`Reiniciar_RigorCore.bat` reinicia solamente el agente de esta instalacion.
No se configura el inicio automatico con Windows.

Para ver errores de conexion, sal del icono con **Exit and disconnect** y ejecuta:

```powershell
.\.venv\Scripts\python.exe -m rigorcore_dev.cli agent
```

Para WSA/Android instala por separado Android SDK Platform Tools y agrega `adb`
al PATH. Los programas usados por los comandos (Git, compiladores, etc.) tambien
deben estar instalados en esta PC.
