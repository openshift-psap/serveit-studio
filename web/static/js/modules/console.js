// console.js — Console log display, save, restore, clear

function logToConsole(message, type = 'info') {
    const line = document.createElement('div');
    line.className = `console-line ${type}`;
    line.textContent = message;
    const consoleEl = document.getElementById('console-output');
    consoleEl.appendChild(line);
    consoleEl.scrollTop = consoleEl.scrollHeight;

    // Persist to localStorage
    saveConsoleMessage(message, type);
}

// Save console message to localStorage
function saveConsoleMessage(message, type) {
    const consoleHistory = JSON.parse(localStorage.getItem('serveit-console') || '[]');
    consoleHistory.push({ message, type, timestamp: Date.now() });
    localStorage.setItem('serveit-console', JSON.stringify(consoleHistory));
}

// Save full console log from database to txt file
function saveConsoleToFile() {
    fetch('/api/logs?limit=100000')
        .then(function(r) { return r.json(); })
        .then(function(logs) {
            if (!logs || !logs.length) {
                alert('Console is empty, nothing to save.');
                return;
            }
            var timestamp = new Date().toISOString().replace(/[:.]/g, '-');
            var content = 'ServeIt Studio Console Log\n';
            content += 'Generated: ' + new Date().toLocaleString() + '\n';
            content += 'Total Messages: ' + logs.length + '\n';
            content += '='.repeat(80) + '\n\n';
            logs.forEach(function(entry) {
                var ts = entry.timestamp || '';
                var typeLabel = (entry.log_type || 'info').toUpperCase();
                while (typeLabel.length < 8) typeLabel += ' ';
                content += '[' + ts + '] [' + typeLabel + '] ' + (entry.message || '') + '\n';
            });
            var blob = new Blob([content], { type: 'text/plain' });
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url;
            a.download = 'serveit-console-log-' + timestamp + '.txt';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        })
        .catch(function(err) {
            console.error('Failed to download logs:', err);
            alert('Failed to download console logs.');
        });
}

// Restore console from localStorage
function restoreConsole() {
    const consoleHistory = JSON.parse(localStorage.getItem('serveit-console') || '[]');
    const consoleEl = document.getElementById('console-output');

    // Only clear and restore if there's history
    if (consoleHistory.length > 0) {
        // Clear existing content
        consoleEl.innerHTML = '';

        // Restore messages
        consoleHistory.forEach(entry => {
            const line = document.createElement('div');
            line.className = `console-line ${entry.type || 'info'}`;
            line.textContent = entry.message;
            consoleEl.appendChild(line);
        });

        consoleEl.scrollTop = consoleEl.scrollHeight;
    }
}

// Clear console history
function clearConsole() {
    // Clear localStorage
    localStorage.removeItem('serveit-console');

    // Clear database via API
    fetch('/api/clear_console', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Clear UI immediately (will be replicated to all clients via socket)
            document.getElementById('console-output').innerHTML = '<div class="console-line">Console cleared.</div>';
        } else {
            console.error('Failed to clear console:', data.error);
        }
    })
    .catch(err => {
        console.error('Error clearing console:', err);
        // Clear UI anyway
        document.getElementById('console-output').innerHTML = '<div class="console-line">Console cleared.</div>';
    });
}

// Save config to server (persisted in database)
