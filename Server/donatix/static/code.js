/* Подсветка кода в стиле терминала и кнопка «Копировать».
   Блоки: <pre data-lang="bash|json|python|js|http">. Без внешних библиотек:
   одно регулярное выражение на язык, каждый фрагмент экранируется отдельно. */
(function () {
  var KW = {
    python: "import|from|def|return|if|else|elif|for|in|not|and|or|raise|try|except|with|as|class|None|True|False|async|await|lambda",
    js: "const|let|var|function|return|if|else|for|of|in|new|throw|await|async|try|catch|true|false|null|undefined|import|from|export",
    bash: "curl|export|echo|sudo|cd|git|python3?",
  };
  var RULES = {
    json: /("(?:\\.|[^"\\])*")(\s*:)?|(-?\b\d+(?:\.\d+)?\b)|\b(true|false|null)\b|([{}\[\],:])/g,
    bash: /(#[^\n]*)|("(?:\\.|[^"\\])*"|'[^']*')|(\$\(?[A-Za-z_][\w]*\)?)|(\s-{1,2}[A-Za-z][\w-]*)|(https?:\/\/[^\s"'\\]+)|\b(KW)\b/g,
    python: /(#[^\n]*)|([rbf]?"(?:\\.|[^"\\])*"|[rbf]?'(?:\\.|[^'\\])*')|\b(KW)\b|(\b\d+(?:\.\d+)?\b)|\b([A-Za-z_]\w*)(?=\()/g,
    js: /(\/\/[^\n]*)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|\b(KW)\b|(\b\d+(?:\.\d+)?\b)|\b([A-Za-z_$][\w$]*)(?=\()/g,
    http: /^(GET|POST|PUT|PATCH|DELETE)\b|^([A-Za-z-]+)(:)|("(?:\\.|[^"\\])*")(\s*:)?|(-?\b\d+(?:\.\d+)?\b)|(https?:\/\/[^\s"]+)/gm,
  };
  // Какой класс получает каждая группа регулярки
  var CLASSES = {
    json: ["str", "punct", "num", "kw", "punct"],
    bash: ["com", "str", "var", "flag", "url", "fn"],
    python: ["com", "str", "kw", "num", "fn"],
    js: ["com", "str", "kw", "num", "fn"],
    http: ["kw", "key", "punct", "str", "punct", "num", "url"],
  };

  function esc(s) { return s.replace(/[&<>]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]; }); }

  function highlight(text, lang) {
    var src = RULES[lang];
    if (!src) return esc(text);
    var re = new RegExp(src.source.replace("KW", KW[lang] || "\\b\\B"), src.flags);
    var out = "", last = 0, m;
    while ((m = re.exec(text))) {
      if (m[0] === "") { re.lastIndex++; continue; }
      out += esc(text.slice(last, m.index));
      var piece = "";
      for (var g = 1; g < m.length; g++) {
        if (m[g] === undefined) continue;
        var cls = CLASSES[lang][g - 1];
        // В JSON строка перед двоеточием — это ключ
        if ((lang === "json" || lang === "http") && cls === "str" && m[g + 1]) cls = "key";
        piece += '<span class="t-' + cls + '">' + esc(m[g]) + "</span>";
      }
      out += piece || esc(m[0]);
      last = re.lastIndex;
    }
    return out + esc(text.slice(last));
  }

  var LABELS = { bash: "terminal", json: "json", python: "python", js: "node.js", http: "http" };

  document.querySelectorAll("pre[data-lang]").forEach(function (pre) {
    var lang = pre.getAttribute("data-lang");
    var text = pre.textContent;
    pre.innerHTML = highlight(text, lang);
    pre.classList.add("hl");
    if (pre.closest(".window") || pre.hasAttribute("data-bare")) return;

    var term = document.createElement("div");
    term.className = "term";
    var head = document.createElement("div");
    head.className = "term-head";
    head.innerHTML = '<span class="dots"><i></i><i></i><i></i></span><span class="term-title">' +
      esc(pre.getAttribute("data-title") || LABELS[lang] || lang) + '</span>';
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "term-copy";
    btn.textContent = "Копировать";
    btn.addEventListener("click", function () {
      var done = function () { btn.textContent = "Скопировано"; setTimeout(function () { btn.textContent = "Копировать"; }, 1500); };
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, function () {});
    });
    head.appendChild(btn);
    pre.parentNode.insertBefore(term, pre);
    term.appendChild(head);
    term.appendChild(pre);
  });
})();
