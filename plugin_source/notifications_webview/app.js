(function () {
  var activeTab = 'new';
  var lastPayload = null;
  var historyVisibleCount = 20;
  var pendingOpenCommitId = null;
  var openDetailCommitId = null;
  var STORAGE_KEYS = {
    historyCount: 'ankicollab.notifications.historyVisibleCount'
  };

  function storageGet(key, fallbackValue) {
    try {
      var value = window.localStorage.getItem(key);
      return value === null ? fallbackValue : value;
    } catch (_err) {
      return fallbackValue;
    }
  }

  function storageSet(key, value) {
    try {
      window.localStorage.setItem(key, String(value));
    } catch (_err) {
      // Ignore storage failures in restricted webview environments.
    }
  }

  function loadUiState() {
    var rawCount = storageGet(STORAGE_KEYS.historyCount, String(historyVisibleCount));
    var parsedCount = Number(rawCount);
    if (!isNaN(parsedCount) && parsedCount >= 20) {
      historyVisibleCount = parsedCount;
    }

  }

  function saveUiState() {
    storageSet(STORAGE_KEYS.historyCount, historyVisibleCount);
  }

  loadUiState();
  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function eventLabel(eventType) {
    var labels = {
      field_added: 'Field Added',
      field_updated: 'Field Updated',
      field_removed: 'Field Removed',
      tag_added: 'Tag Added',
      tag_removed: 'Tag Removed',
      note_moved: 'Note Moved',
      note_deleted: 'Note Deleted',
      field_change_denied: 'Denied Field Change',
      tag_change_denied: 'Denied Tag Change',
      commit_approved_effect: 'Commit Approved',
      commit_denied_effect: 'Commit Denied'
    };
    return labels[eventType] || eventType;
  }

  function groupByNote(events) {
    var grouped = {};
    (events || []).forEach(function (event) {
      var key = String(event.note_id);
      if (!grouped[key]) {
        grouped[key] = [];
      }
      grouped[key].push(event);
    });
    return grouped;
  }

  function isLowSignalEvent(eventType) {
    return eventType === 'commit_approved_effect' || eventType === 'commit_denied_effect' || eventType === 'suggestion_denied';
  }

  function filterNoteEvents(events) {
    var list = (events || []).slice().sort(function (a, b) {
      var av = Number(a.version || 0);
      var bv = Number(b.version || 0);
      return av - bv;
    });

    var hasMeaningful = list.some(function (event) {
      return !isLowSignalEvent(event.event_type);
    });

    if (!hasMeaningful) {
      return list.slice(-1);
    }

    return list.filter(function (event) {
      return !isLowSignalEvent(event.event_type);
    });
  }

  function activateTab(tabName) {
    var tabNew = document.getElementById('tabNew');
    var tabHistory = document.getElementById('tabHistory');
    var newPanel = document.getElementById('newPanel');
    var historyPanel = document.getElementById('historyPanel');
    var detailPanel = document.getElementById('detailPanel');
    var showNew = tabName === 'new';

    tabNew.classList.toggle('active', showNew);
    tabHistory.classList.toggle('active', !showNew);
    tabNew.setAttribute('aria-selected', showNew ? 'true' : 'false');
    tabHistory.setAttribute('aria-selected', showNew ? 'false' : 'true');
    newPanel.classList.toggle('hidden', !showNew);
    historyPanel.classList.toggle('hidden', showNew);
    if (detailPanel) {
      detailPanel.classList.add('hidden');
    }
    openDetailCommitId = null;
    activeTab = tabName;
    saveUiState();
  }

  function openDetailView(item, snapshot, payload) {
    var detailPanel = document.getElementById('detailPanel');
    var newPanel = document.getElementById('newPanel');
    var historyPanel = document.getElementById('historyPanel');
    if (!detailPanel) {
      return;
    }

    var html = [];
    html.push('<div class="detail-header">');
    html.push('<button id="detailBackBtn" class="detail-back-btn" type="button" aria-label="Back to history"><span class="back-arrow" aria-hidden="true"></span> Back</button>');
    html.push('<div class="detail-title">Review Details</div>');
    html.push('</div>');

    html.push('<div class="detail-box detail-standalone">');
    html.push('<div class="snapshot-meta"><span class="snapshot-label">Author:</span> ' + escapeHtml(snapshot.author || 'Unknown') + '</div>');
    html.push('<div class="snapshot-meta"><span class="snapshot-label">Deck:</span> ' + escapeHtml(snapshot.deck_name || '') + '</div>');
    html.push('<div class="snapshot-meta"><span class="snapshot-label">Date:</span> ' + escapeHtml(formatTime(snapshot.timestamp || '')) + '</div>');
    if (snapshot.info) {
      html.push('<div class="snapshot-meta">Message: ' + escapeHtml(snapshot.info) + '</div>');
    }
    html.push(renderDecisionBanner(snapshot));

    var events = snapshot.events || [];
    if (events.length === 0) {
      html.push('<div class="snapshot-empty">No immutable note events were found for this commit.</div>');
    } else {
      var grouped = groupByNote(events);
      var sortedNoteIds = Object.keys(grouped).sort(function (a, b) {
        return Number(a) - Number(b);
      });

      sortedNoteIds.forEach(function (noteId, index) {
        var filtered = filterNoteEvents(grouped[noteId]);
        if (filtered.length === 0) {
          return;
        }
        var noteGuid = '';
        for (var gi = 0; gi < filtered.length; gi++) {
          if (filtered[gi] && filtered[gi].note_guid) {
            noteGuid = String(filtered[gi].note_guid);
            break;
          }
        }

        html.push('<details class="note-section" open>');
        html.push('<summary class="note-divider"><span class="note-divider-title">Card Update ' + String(index + 1) + '</span>');
        if (noteGuid) {
          html.push('<a class="note-open-link note-open-link-inline" onclick="event.stopPropagation();" href="ankicollab-open-guid://open?guid=' + encodeURIComponent(noteGuid) + '">Open in Anki</a>');
        }
        html.push('</summary>');
        html.push('<div class="note-content">');

        var tagAdded = [];
        var tagRemoved = [];
        var tagDenied = [];
        filtered.forEach(function (event) {
          if (event.event_type === 'tag_added') {
            tagAdded.push(event.new_text || event.old_text || 'Tag');
          } else if (event.event_type === 'tag_removed') {
            tagRemoved.push(event.new_text || event.old_text || 'Tag');
          } else if (event.event_type === 'tag_change_denied') {
            tagDenied.push(event.new_text || event.old_text || 'Tag change denied');
          } else {
            html.push(renderNoteEvent(event));
          }
        });

        if (tagAdded.length > 0 || tagRemoved.length > 0 || tagDenied.length > 0) {
          html.push(renderTagSummary(tagAdded, tagRemoved, tagDenied));
        }

        html.push('</div>');
        html.push('</details>');
      });
    }
    html.push('</div>');

    detailPanel.innerHTML = html.join('');
    resolveMediaIn(detailPanel, payload.media_dir || '');
    newPanel.classList.add('hidden');
    historyPanel.classList.add('hidden');
    detailPanel.classList.remove('hidden');
    openDetailCommitId = Number(item.commit_id);

    var backBtn = document.getElementById('detailBackBtn');
    if (backBtn) {
      backBtn.onclick = function () {
        detailPanel.classList.add('hidden');
        historyPanel.classList.remove('hidden');
        activateTab('history');
      };
      backBtn.focus();
    }
  }

  function clearNode(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function syncOverlayBounds() {
    var overlay = document.getElementById('loadingOverlay');
    if (!overlay) {
      return;
    }
    var w = Math.max(document.documentElement.clientWidth || 0, window.innerWidth || 0);
    var h = Math.max(document.documentElement.clientHeight || 0, window.innerHeight || 0);
    overlay.style.width = String(w) + 'px';
    overlay.style.height = String(h) + 'px';
  }

  function formatTime(value) {
    var raw = String(value || '').trim();
    if (!raw) {
      return '';
    }

    var normalized = raw;
    if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(normalized)) {
      normalized = normalized.replace(' ', 'T');
    }
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}$/.test(normalized)) {
      normalized += ':00';
    }

    var parsed = new Date(normalized);
    if (isNaN(parsed.getTime())) {
      return raw.replace('T', ' ');
    }
    var datePart = parsed.toLocaleDateString([], {
      year: 'numeric',
      month: 'short',
      day: '2-digit'
    });
    var timePart = parsed.toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    });
    return datePart + ' at ' + timePart;
  }

  function renderDecisionBanner(snapshot) {
    if (!snapshot.decision_status) {
      return '';
    }
    var isApproved = snapshot.decision_status === 'approved';
    var text = isApproved ? 'Approved' : 'Denied';
    var html = '';
    html += '<div class="decision-banner ' + (isApproved ? 'approved' : 'denied') + '">';
    html += '<div class="decision-status">' + text + '</div>';
    if (snapshot.decision_reason) {
      html += '<div class="decision-text">' + escapeHtml(snapshot.decision_reason) + '</div>';
    }
    html += '</div>';
    return html;
  }

  function toSplitDiffHtml(diffHtml) {
    var source = diffHtml || '';
    var left = source
      .replace(/<ins[^>]*>[\s\S]*?<\/ins>/gi, '')
      .replace(/<ins[^>]*\/>/gi, '')
      .replace(/<del[^>]*>/gi, '<span class="diff-removed">')
      .replace(/<\/del>/gi, '</span>');

    var right = source
      .replace(/<del[^>]*>[\s\S]*?<\/del>/gi, '')
      .replace(/<del[^>]*\/>/gi, '')
      .replace(/<ins[^>]*>/gi, '<span class="diff-added">')
      .replace(/<\/ins>/gi, '</span>');

    return { left: left, right: right };
  }

  function resolveMediaIn(container, mediaDir) {
    if (!container || !mediaDir) {
      return;
    }
    var normalizedBase = String(mediaDir).replace(/\\/g, '/').replace(/\/$/, '');
    var images = container.querySelectorAll('img');
    images.forEach(function (img) {
      var src = img.getAttribute('src') || '';
      if (!src) {
        return;
      }
      // Block external http(s) images to avoid remote network requests in the notification UI.
      if (/^https?:\/\//i.test(src)) {
        img.setAttribute('data-blocked-src', src);
        img.removeAttribute('src');
        if (!img.hasAttribute('alt')) {
          img.setAttribute('alt', '[external image blocked]');
        }
        return;
      }
      // Leave data: and file: URLs untouched.
      if (/^data:/i.test(src) || /^file:/i.test(src)) {
        return;
      }
      // Decode once so that any encoded separators or traversal sequences can be validated.
      var decodedSrc;
      try {
        decodedSrc = decodeURIComponent(src);
      } catch (_e) {
        decodedSrc = src;
      }
      // Only allow simple filenames: no path separators and no ".." segments.
      if (/[\/\\]/.test(decodedSrc) || decodedSrc.indexOf('..') !== -1) {
        return;
      }
      var encodedName = encodeURIComponent(decodedSrc);
      img.setAttribute('src', 'file:///' + normalizedBase + '/' + encodedName);
      img.style.maxWidth = '260px';
      img.style.maxHeight = '160px';
      img.style.width = 'auto';
      img.style.height = 'auto';
      img.style.objectFit = 'contain';
      img.style.display = 'block';
    });
  }

  function fieldHtml(value, fallbackText) {
    if (typeof value === 'string' && value.trim().length > 0) {
      return '<span class="rich-content-sanitized">' + escapeHtml(value) + '</span>';
    }
    return '<span class="event-empty">' + escapeHtml(fallbackText) + '</span>';
  }

  function normalizeTagLabel(value) {
    return String(value || '').replace(/^#+/, '').trim();
  }

  function renderTagEvent(event) {
    var eventType = event.event_type;
    var isAdd = eventType === 'tag_added';
    var isDenied = eventType === 'tag_change_denied';
    var label = normalizeTagLabel(event.new_text || event.old_text || 'Tag change');
    var html = '';
    html += '<div class="event-card">';
    html += '<div class="tag-row">';
    html += '<span class="tag-pill ' + (isDenied ? 'tag-denied' : isAdd ? 'tag-added' : 'tag-removed') + '">';
    html += (isDenied ? 'Denied' : isAdd ? 'Added' : 'Removed') + ': ' + escapeHtml(label);
    html += '</span>';
    html += '</div>';
    html += '</div>';
    return html;
  }

  function renderTagSummary(tagAdded, tagRemoved, tagDenied) {
    function chips(tags, cls) {
      var shown = tags.slice(0, 4);
      var html = '<div class="tag-chip-wrap">';
      shown.forEach(function (tag) {
        html += '<span class="tag-pill ' + cls + '">' + escapeHtml(normalizeTagLabel(tag)) + '</span>';
      });
      if (tags.length > shown.length) {
        html += '<span class="tag-pill tag-more">+' + String(tags.length - shown.length) + '</span>';
      }
      html += '</div>';
      return html;
    }

    var html = '';
    html += '<div class="event-card tag-summary-inline">';
    if (tagAdded.length > 0) {
      html += '<div class="tag-summary-row"><span class="tag-summary-label">Added</span>' + chips(tagAdded, 'tag-added') + '</div>';
    }
    if (tagRemoved.length > 0) {
      html += '<div class="tag-summary-row"><span class="tag-summary-label">Removed</span>' + chips(tagRemoved, 'tag-removed') + '</div>';
    }
    if (tagDenied.length > 0) {
      html += '<div class="tag-summary-row"><span class="tag-summary-label">Denied</span>' + chips(tagDenied, 'tag-denied') + '</div>';
    }
    html += '</div>';
    return html;
  }

  function renderFieldEvent(event) {
    var oldText = event.old_text || '';
    var newText = event.new_text || '';
    var eventType = event.event_type;
    var leftTitle = 'Before';
    var rightTitle = 'After';
    var leftBody = fieldHtml(oldText, 'No prior value');
    var rightBody = fieldHtml(newText, 'No new value');

    if (eventType === 'field_updated' && event.diff_html) {
      var split = toSplitDiffHtml(event.diff_html);
      leftBody = split.left || leftBody;
      rightBody = split.right || rightBody;
    }

    var html = '';
    html += '<div class="event-card">';
    if (event.field_name) {
      html += '<div class="event-head">';
      html += '<span class="event-field">' + escapeHtml(event.field_name) + '</span>';
      html += '</div>';
    }
    html += '<div class="event-columns split-diff">';
    html += '<div class="event-col old-side"><div class="event-col-title">' + leftTitle + '</div><div class="event-col-body rich-content">' + leftBody + '</div></div>';
    html += '<div class="event-col new-side"><div class="event-col-title">' + rightTitle + '</div><div class="event-col-body rich-content">' + rightBody + '</div></div>';
    html += '</div>';
    html += '</div>';
    return html;
  }

  function renderMoveEvent(event) {
    var html = '';
    html += '<div class="event-card">';
    html += '<div class="move-line">' + escapeHtml(event.old_text || 'Unknown deck') + ' → ' + escapeHtml(event.new_text || 'Unknown deck') + '</div>';
    html += '</div>';
    return html;
  }

  function renderNoteEvent(event) {
    if (event.event_type === 'tag_added' || event.event_type === 'tag_removed' || event.event_type === 'tag_change_denied') {
      return renderTagEvent(event);
    }
    if (event.event_type === 'note_moved') {
      return renderMoveEvent(event);
    }
    return renderFieldEvent(event);
  }

  function buildEmptyState(iconChar, title, subtextLines) {
    var wrap = document.createElement('div');
    wrap.className = 'empty';
    if (iconChar) {
      var icon = document.createElement('span');
      icon.className = 'empty-icon';
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = iconChar;
      wrap.appendChild(icon);
    }
    var heading = document.createElement('div');
    heading.className = 'empty-title';
    heading.textContent = title;
    wrap.appendChild(heading);
    var sub = document.createElement('div');
    sub.className = 'empty-sub';
    subtextLines.forEach(function (line, idx) {
      if (idx > 0) {
        sub.appendChild(document.createElement('br'));
      }
      sub.appendChild(document.createTextNode(line));
    });
    wrap.appendChild(sub);
    return wrap;
  }

  function render(payload) {
    if (typeof payload === 'string') {
      try {
        payload = JSON.parse(payload);
      } catch (_parseErr) {
        payload = {};
      }
    }
    payload = payload || {};

    try {
    if (lastPayload !== payload) {
      var nextItems = (payload.history && payload.history.items) || [];
      var prevItems = (lastPayload && lastPayload.history && lastPayload.history.items) || [];
      if (nextItems !== prevItems) {
        historyVisibleCount = 20;
      }
    }
    lastPayload = payload;

    var loadingOverlay = document.getElementById('loadingOverlay');
    var root = document.querySelector('.wrap');
    if (loadingOverlay) {
      loadingOverlay.classList.toggle('hidden', !payload.loading);
      if (payload.loading) {
        syncOverlayBounds();
      }
    }
    if (root) {
      root.classList.toggle('is-loading', !!payload.loading);
      root.setAttribute('aria-busy', payload.loading ? 'true' : 'false');
    }

    var unread = payload.unread || { unread_count: 0, groups: [] };
    var history = payload.history || { items: [] };
    var snapshots = payload.commit_snapshots || {};

    var unreadBadge = document.getElementById('unreadBadge');
    var unreadCount = Number(unread.unread_count || 0);
    var unreadLabel = 'No new';
    if (unreadCount > 0 && unreadCount <= 10) {
      unreadLabel = String(unreadCount) + ' new';
    } else if (unreadCount > 10 && unreadCount <= 99) {
      unreadLabel = '10+ new';
    } else if (unreadCount > 99) {
      unreadLabel = '99+ new';
    }
    unreadBadge.textContent = unreadLabel;
    unreadBadge.classList.toggle('has-unread', unreadCount > 0);

    var tabNew = document.getElementById('tabNew');
    var tabHistory = document.getElementById('tabHistory');
    if (!tabNew || !tabHistory) {
      setTimeout(function () { render(payload); }, 50);
      return;
    }
    tabNew.disabled = !!payload.loading;
    tabHistory.disabled = !!payload.loading;
    tabNew.onclick = function () { activateTab('new'); };
    tabHistory.onclick = function () { activateTab('history'); };

    var refreshBtn = document.getElementById('refreshBtn');
    if (refreshBtn) {
      refreshBtn.disabled = !!payload.loading;
      refreshBtn.classList.toggle('loading', !!payload.loading);
      refreshBtn.onclick = function () {
        if (payload.loading) {
          return;
        }
        window.location.href = 'ankicollab-refresh://now';
      };
    }

    var newPanel = document.getElementById('newPanel');
    var historyPanel = document.getElementById('historyPanel');
    if (!newPanel || !historyPanel) {
      setTimeout(function () { render(payload); }, 50);
      return;
    }

    var focusedBefore = document.activeElement;
    var focusWasInDynamic = focusedBefore && (newPanel.contains(focusedBefore) || historyPanel.contains(focusedBefore));
    if (focusWasInDynamic) {
      (activeTab === 'history' ? tabHistory : tabNew).focus();
    }

    var detailPanel = document.getElementById('detailPanel');
    clearNode(newPanel);
    clearNode(historyPanel);
    if (detailPanel) {
      clearNode(detailPanel);
    }

    var groups = Array.isArray(unread.groups) ? unread.groups : [];
    if (groups.length === 0) {
      var emptyNew = buildEmptyState(null, 'All caught up', [
        'No new notifications right now.',
        "We'll let you know when something changes with your suggestions."
      ]);
      newPanel.appendChild(emptyNew);
    } else {
      var historyItemsForJump = history.items || [];

      function jumpToCommit(commitId) {
        var targetId = Number(commitId);
        var targetIdx = historyItemsForJump.findIndex(function (entry) {
          return Number(entry.commit_id) === targetId;
        });
        if (targetIdx >= 0 && historyVisibleCount < targetIdx + 1) {
          historyVisibleCount = targetIdx + 1;
        }
        pendingOpenCommitId = targetId;
        activeTab = 'history';
        render(lastPayload || payload);
      }

      groups.forEach(function (group) {
        var row = document.createElement('div');
        row.className = 'group';

        var title = document.createElement('div');
        title.className = 'group-title';
        title.textContent = group.deck_name + ':';

        var summary = document.createElement('div');
        summary.className = 'group-summary';
        summary.textContent = String(group.approved_count || 0) + ' suggestions approved, ' + String(group.denied_count || 0) + ' denied.';

        row.appendChild(title);
        row.appendChild(summary);

        var actionsWrap = document.createElement('div');
        actionsWrap.className = 'new-actions-wrap';
        (group.notifications || []).forEach(function (entry) {
          var actionRow = document.createElement('div');
          actionRow.className = 'new-action-row';
          actionRow.setAttribute('role', 'button');
          actionRow.setAttribute('tabindex', '0');

          var actionLeft = document.createElement('div');
          actionLeft.className = 'new-action-left';

          var statusChip = document.createElement('span');
          statusChip.className = 'new-status-chip ' + (entry.status === 'approved' ? 'approved' : 'denied');
          statusChip.textContent = entry.status === 'approved' ? 'Approved' : 'Denied';

          var actionText = document.createElement('span');
          actionText.className = 'new-action-text';
          actionText.textContent = formatTime(entry.created_at);

          actionLeft.appendChild(statusChip);
          actionLeft.appendChild(actionText);

          actionRow.onclick = function () {
            jumpToCommit(entry.commit_id);
          };
          actionRow.onkeydown = function (evt) {
            if (evt.key === 'Enter' || evt.key === ' ') {
              evt.preventDefault();
              jumpToCommit(entry.commit_id);
            }
          };

          actionRow.appendChild(actionLeft);
          actionsWrap.appendChild(actionRow);
        });

        row.appendChild(actionsWrap);
        newPanel.appendChild(row);
      });
    }

    var items = Array.isArray(history.items) ? history.items : [];
    var visibleItems = items.slice(0, historyVisibleCount);
    if (items.length === 0) {
      var emptyHistory = buildEmptyState(
        '\uD83D\uDD53\uFE0E',
        'No history yet',
        ['Your review history will appear here once', 'suggestions have been processed.']
      );
      historyPanel.appendChild(emptyHistory);
    } else {
      visibleItems.forEach(function (item) {
        var row = document.createElement('div');
        row.className = 'timeline-item timeline-entry' + (item.status === 'approved' ? ' status-line-approved' : ' status-line-denied');
        row.setAttribute('data-commit-id', String(item.commit_id));

        var status = document.createElement('div');
        status.className = item.status === 'approved' ? 'status-approved' : 'status-denied';
        status.textContent = item.status === 'approved' ? 'Approved' : 'Denied';

        var meta = document.createElement('div');
        meta.className = 'meta';
        meta.textContent = item.deck_name + ' • ' + formatTime(item.created_at);

        row.appendChild(status);
        row.appendChild(meta);

        if (item.reason) {
          var reason = document.createElement('div');
          reason.className = 'reason';
          reason.textContent = item.reason;
          row.appendChild(reason);
        }

        var snapshot = snapshots[String(item.commit_id)];
        if (snapshot) {
          row.classList.add('timeline-clickable');
          row.setAttribute('role', 'button');
          row.setAttribute('tabindex', '0');
          row.onclick = function () {
            openDetailView(item, snapshot, payload);
          };
          row.onkeydown = function (evt) {
            if (evt.key === 'Enter' || evt.key === ' ') {
              evt.preventDefault();
              openDetailView(item, snapshot, payload);
            }
          };

          if (pendingOpenCommitId !== null && Number(item.commit_id) === Number(pendingOpenCommitId)) {
            pendingOpenCommitId = null;
            setTimeout(function () {
              openDetailView(item, snapshot, payload);
            }, 0);
          }
        } else {
          row.classList.add('timeline-disabled');
          var unavailable = document.createElement('div');
          unavailable.className = 'timeline-unavailable';
          unavailable.textContent = 'Details unavailable';
          row.appendChild(unavailable);
        }

        historyPanel.appendChild(row);
      });

      if (items.length > visibleItems.length) {
        var loadMore = document.createElement('button');
        loadMore.type = 'button';
        loadMore.className = 'detail-btn load-more-btn';
        loadMore.textContent = 'Load More';
        loadMore.onclick = function () {
          historyVisibleCount += 20;
          saveUiState();
          render(lastPayload || payload);
          activateTab('history');
        };
        historyPanel.appendChild(loadMore);
      }
    }

    activateTab(activeTab);
    saveUiState();
    } catch (err) {
      var errText = (err && err.message) ? err.message : 'Unknown render error';
      var errEl = buildEmptyState('\u26A0\uFE0E', 'Something went wrong', [errText]);
      var fallbackNew = document.getElementById('newPanel');
      var fallbackHistory = document.getElementById('historyPanel');
      if (fallbackNew) {
        clearNode(fallbackNew);
        fallbackNew.appendChild(errEl.cloneNode(true));
      }
      if (fallbackHistory) {
        clearNode(fallbackHistory);
        fallbackHistory.appendChild(errEl.cloneNode(true));
      }
    }
  }

  window.AnkiCollabNotifications = {
    render: render
  };

  window.addEventListener('resize', syncOverlayBounds);
})();
