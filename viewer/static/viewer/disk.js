(function () {
    'use strict';

    var CONFIG = window.VIEWER || {};
    var MAX_CONCURRENT = 3;

    function $(selector, root) {
        return (root || document).querySelector(selector);
    }

    function $all(selector, root) {
        return Array.prototype.slice.call((root || document).querySelectorAll(selector));
    }

    function getCsrfToken() {
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) {
            return input.value;
        }
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : '';
    }

    function toast(message, type) {
        var container = $('#toast-container');
        if (!container) {
            return;
        }
        var node = document.createElement('div');
        node.className = 'toast ' + (type || 'info');
        node.textContent = message;
        container.appendChild(node);
        window.setTimeout(function () {
            node.classList.add('toast-out');
            window.setTimeout(function () {
                node.remove();
            }, 200);
        }, 4200);
    }

    /* ---------- Theme ---------- */
    var root = document.documentElement;

    function effectiveTheme() {
        if (root.dataset.theme === 'dark' || root.dataset.theme === 'light') {
            return root.dataset.theme;
        }
        return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }

    function applyTheme(theme) {
        root.dataset.theme = theme;
        try {
            localStorage.setItem('yd-theme', theme);
        } catch (e) {}
    }

    var themeToggle = $('#theme-toggle');
    if (themeToggle) {
        themeToggle.addEventListener('click', function () {
            applyTheme(effectiveTheme() === 'dark' ? 'light' : 'dark');
        });
    }

    /* ---------- Sidebar ---------- */
    var sidebar = $('#sidebar');
    var backdrop = $('#sidebar-backdrop');
    var menuButton = $('#menu-button');

    function setSidebar(open) {
        if (!sidebar || !backdrop) {
            return;
        }
        sidebar.classList.toggle('open', open);
        backdrop.classList.toggle('active', open);
    }

    if (menuButton) {
        menuButton.addEventListener('click', function () {
            setSidebar(!sidebar.classList.contains('open'));
        });
    }
    if (backdrop) {
        backdrop.addEventListener('click', function () {
            setSidebar(false);
        });
    }

    /* ---------- View mode / search / sort ---------- */
    function currentTable() {
        return $('#file-table');
    }

    function applyView(view) {
        var table = currentTable();
        if (!table) {
            return;
        }
        var next = view === 'grid' ? 'grid' : 'list';
        table.dataset.view = next;
        try {
            localStorage.setItem('yd-view', next);
        } catch (e) {}
        var listButton = $('#view-list');
        var gridButton = $('#view-grid');
        if (listButton) {
            listButton.classList.toggle('active', next === 'list');
        }
        if (gridButton) {
            gridButton.classList.toggle('active', next === 'grid');
        }
    }

    function applySearch() {
        var input = $('#file-search');
        var table = currentTable();
        if (!input || !table) {
            return;
        }
        var query = input.value.trim().toLowerCase();
        $all('.file-row', table).forEach(function (row) {
            var name = row.dataset.name || '';
            row.hidden = query !== '' && name.indexOf(query) === -1;
        });
    }

    var viewListButton = $('#view-list');
    var viewGridButton = $('#view-grid');
    if (viewListButton) {
        viewListButton.addEventListener('click', function () {
            applyView('list');
        });
    }
    if (viewGridButton) {
        viewGridButton.addEventListener('click', function () {
            applyView('grid');
        });
    }

    var searchInput = $('#file-search');
    if (searchInput) {
        searchInput.addEventListener('input', applySearch);
    }

    var sortSelect = $('#sort-select');
    if (sortSelect) {
        sortSelect.addEventListener('change', function () {
            var parts = sortSelect.value.split(':');
            var url = new URL(window.location.href);
            url.searchParams.set('sort', parts[0]);
            url.searchParams.set('order', parts[1]);
            url.searchParams.delete('page');
            window.location.href = url.toString();
        });
    }

    /* ---------- Selection ---------- */
    function selectedBoxes() {
        return $all('input[name="selected_files"]').filter(function (box) {
            return !box.closest('[hidden]');
        });
    }

    function updateSelection() {
        var boxes = selectedBoxes();
        var checked = boxes.filter(function (box) {
            return box.checked;
        });
        var bar = $('#selection-bar');
        var counter = $('#selection-count');
        var selectAll = $('#select-all');
        var zipButton = $('#zip-download-button');

        if (bar) {
            bar.hidden = checked.length === 0;
        }
        if (counter) {
            counter.textContent = String(checked.length);
        }
        if (zipButton) {
            zipButton.disabled = checked.length === 0;
        }
        if (selectAll) {
            selectAll.checked = boxes.length > 0 && checked.length === boxes.length;
            selectAll.indeterminate = checked.length > 0 && checked.length < boxes.length;
        }
    }

    document.addEventListener('change', function (event) {
        var target = event.target;
        if (target.matches && target.matches('#select-all')) {
            var checked = target.checked;
            selectedBoxes().forEach(function (box) {
                box.checked = checked;
            });
            updateSelection();
        } else if (target.matches && target.matches('input[name="selected_files"]')) {
            updateSelection();
        }
    });

    var selectionClear = $('#selection-clear');
    if (selectionClear) {
        selectionClear.addEventListener('click', function () {
            selectedBoxes().forEach(function (box) {
                box.checked = false;
            });
            updateSelection();
        });
    }

    /* ---------- Row navigation / preview ---------- */
    document.addEventListener('click', function (event) {
        var row = event.target.closest && event.target.closest('.file-row.is-dir');
        if (row && !event.target.closest('a, button, input, .check-cell')) {
            var href = row.getAttribute('data-href');
            if (href) {
                window.location.href = href;
            }
        }
    });

    function resetMediaElement(element) {
        if (!element) {
            return;
        }
        try {
            element.pause();
        } catch (e) {}
        element.hidden = true;
        element.removeAttribute('src');
        try {
            element.load();
        } catch (e) {}
    }

    var galleryItems = [];
    var galleryIndex = -1;

    function modalIsOpen() {
        var modal = $('#imagePreviewModal');
        return !!(modal && modal.classList.contains('open'));
    }

    function closeMediaModal() {
        var modal = $('#imagePreviewModal');
        var image = $('#modalImage');
        var caption = $('#modalCaption');
        if (!modal) {
            return;
        }
        modal.classList.remove('open');
        if (image) {
            image.hidden = false;
            image.removeAttribute('src');
        }
        resetMediaElement($('#modalVideo'));
        resetMediaElement($('#modalAudio'));
        if (caption) {
            caption.textContent = '';
        }
        galleryItems = [];
        galleryIndex = -1;
        updateGalleryChrome();
    }

    function setMediaContent(kind, url, title) {
        var image = $('#modalImage');
        var video = $('#modalVideo');
        var audio = $('#modalAudio');
        var caption = $('#modalCaption');
        if (!image) {
            return;
        }

        resetMediaElement(video);
        resetMediaElement(audio);
        image.hidden = true;
        image.removeAttribute('src');

        if (kind === 'video' && video) {
            video.hidden = false;
            video.src = url;
            var playResult = video.play();
            if (playResult && playResult.catch) {
                playResult.catch(function () {});
            }
        } else if (kind === 'audio' && audio) {
            audio.hidden = false;
            audio.src = url;
            var audioPlay = audio.play();
            if (audioPlay && audioPlay.catch) {
                audioPlay.catch(function () {});
            }
        } else {
            image.hidden = false;
            image.src = url;
        }

        if (caption) {
            caption.textContent = title || '';
        }
    }

    function collectGallery() {
        var nodes = $all('.preview-trigger, .file-preview');
        var items = [];
        var seen = {};
        nodes.forEach(function (node) {
            var url = node.dataset.previewUrl || node.dataset.fullsize;
            if (!url || seen[url]) {
                return;
            }
            var row = node.closest('.file-row');
            if ((row && row.hidden) || node.closest('[hidden]')) {
                return;
            }
            seen[url] = true;
            items.push({
                url: url,
                kind: node.dataset.mediaKind || 'image',
                title: node.dataset.title || node.getAttribute('alt') || ''
            });
        });
        return items;
    }

    function updateGalleryChrome() {
        var counter = $('#galleryCounter');
        var prevButton = $('#galleryPrev');
        var nextButton = $('#galleryNext');
        var total = galleryItems.length;
        if (counter) {
            counter.hidden = total <= 1 || galleryIndex < 0;
            counter.textContent = counter.hidden ? '' : (galleryIndex + 1) + ' / ' + total;
        }
        if (prevButton) {
            prevButton.hidden = total <= 1;
        }
        if (nextButton) {
            nextButton.hidden = total <= 1;
        }
    }

    function showGalleryItem(index) {
        if (galleryItems.length === 0) {
            return;
        }
        galleryIndex = ((index % galleryItems.length) + galleryItems.length) % galleryItems.length;
        var item = galleryItems[galleryIndex];

        var modal = $('#imagePreviewModal');
        if (modal && !modal.classList.contains('open')) {
            modal.classList.add('open');
            var closeButton = $('.modal-close', modal);
            if (closeButton) {
                closeButton.focus();
            }
        }

        setMediaContent(item.kind, item.url, item.title);
        updateGalleryChrome();
    }

    function openGallery(url, kind, title) {
        galleryItems = collectGallery();
        var index = -1;
        for (var i = 0; i < galleryItems.length; i += 1) {
            if (galleryItems[i].url === url) {
                index = i;
                break;
            }
        }
        if (index === -1) {
            galleryItems.push({ url: url, kind: kind || 'image', title: title || '' });
            index = galleryItems.length - 1;
        }
        showGalleryItem(index);
    }

    document.addEventListener('click', function (event) {
        var trigger = event.target.closest && event.target.closest('.preview-trigger, .file-preview');
        if (trigger) {
            event.preventDefault();
            var url = trigger.dataset.previewUrl || trigger.dataset.fullsize;
            if (!url) {
                return;
            }
            var kind = trigger.dataset.mediaKind || 'image';
            var title = trigger.dataset.title || trigger.getAttribute('alt') || '';
            openGallery(url, kind, title);
            return;
        }
        var modal = $('#imagePreviewModal');
        if (modal && modal.classList.contains('open') && event.target === modal) {
            closeMediaModal();
        }
    });

    var modalCloseButton = $('.modal-close');
    if (modalCloseButton) {
        modalCloseButton.addEventListener('click', closeMediaModal);
    }

    var galleryPrevButton = $('#galleryPrev');
    if (galleryPrevButton) {
        galleryPrevButton.addEventListener('click', function () {
            showGalleryItem(galleryIndex - 1);
        });
    }
    var galleryNextButton = $('#galleryNext');
    if (galleryNextButton) {
        galleryNextButton.addEventListener('click', function () {
            showGalleryItem(galleryIndex + 1);
        });
    }

    var mediaModal = $('#imagePreviewModal');
    if (mediaModal) {
        var swipeStartX = null;
        mediaModal.addEventListener('touchstart', function (event) {
            if (event.touches.length === 1) {
                swipeStartX = event.touches[0].clientX;
            }
        }, { passive: true });
        mediaModal.addEventListener('touchend', function (event) {
            if (swipeStartX === null || galleryItems.length <= 1) {
                swipeStartX = null;
                return;
            }
            var touch = event.changedTouches[0];
            var delta = touch ? touch.clientX - swipeStartX : 0;
            swipeStartX = null;
            if (Math.abs(delta) > 60) {
                showGalleryItem(galleryIndex + (delta < 0 ? 1 : -1));
            }
        }, { passive: true });
    }

    /* ---------- Region refresh ---------- */
    function initRegion() {
        var savedView = 'list';
        try {
            savedView = localStorage.getItem('yd-view') || 'list';
        } catch (e) {}
        applyView(savedView);
        applySearch();
        updateSelection();
    }

    function refreshListing() {
        var current = $('#file-list-region');
        if (!current) {
            window.location.reload();
            return;
        }
        return fetch(window.location.href, {
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
            credentials: 'same-origin'
        }).then(function (response) {
            if (!response.ok) {
                throw new Error('refresh failed');
            }
            return response.text();
        }).then(function (html) {
            var doc = new DOMParser().parseFromString(html, 'text/html');
            var fresh = doc.getElementById('file-list-region');
            if (!fresh) {
                return;
            }
            var live = $('#file-list-region');
            if (live) {
                live.replaceWith(fresh);
                initRegion();
            }
        }).catch(function () {});
    }

    var refreshButton = $('#refresh-button');
    if (refreshButton) {
        refreshButton.addEventListener('click', function () {
            refreshButton.classList.add('active');
            Promise.resolve(refreshListing()).then(function () {
                window.setTimeout(function () {
                    refreshButton.classList.remove('active');
                }, 250);
            });
        });
    }

    /* ---------- Folder creation ---------- */
    var folderModal = $('#folder-modal');
    var folderForm = $('#folder-form');
    var folderInput = $('#folder-name');
    var folderError = $('#folder-error');
    var folderSubmit = $('#folder-submit');
    var folderCancel = $('#folder-cancel');

    function openFolderModal() {
        if (!folderModal) {
            return;
        }
        folderModal.classList.add('open');
        if (folderError) {
            folderError.hidden = true;
            folderError.textContent = '';
        }
        if (folderInput) {
            folderInput.value = '';
            window.setTimeout(function () {
                folderInput.focus();
            }, 30);
        }
    }

    function closeFolderModal() {
        if (folderModal) {
            folderModal.classList.remove('open');
        }
    }

    ['#create-folder-button', '#create-folder-button-content'].forEach(function (selector) {
        var button = $(selector);
        if (button) {
            button.addEventListener('click', openFolderModal);
        }
    });
    if (folderCancel) {
        folderCancel.addEventListener('click', closeFolderModal);
    }
    if (folderModal) {
        folderModal.addEventListener('click', function (event) {
            if (event.target === folderModal) {
                closeFolderModal();
            }
        });
    }
    if (folderForm) {
        folderForm.addEventListener('submit', function (event) {
            event.preventDefault();
            var name = folderInput ? folderInput.value.trim() : '';
            if (!name) {
                showFolderError('Введите название папки.');
                return;
            }
            if (folderSubmit) {
                folderSubmit.disabled = true;
            }
            fetch(CONFIG.createFolderUrl, {
                method: 'POST',
                headers: {
                    'X-CSRFToken': getCsrfToken(),
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept': 'application/json'
                },
                credentials: 'same-origin',
                body: new URLSearchParams({
                    name: name,
                    path: CONFIG.currentPath || '/'
                })
            }).then(function (response) {
                return response.json().catch(function () {
                    return {};
                }).then(function (data) {
                    return { ok: response.ok, data: data };
                });
            }).then(function (result) {
                if (result.ok && result.data.ok) {
                    closeFolderModal();
                    toast('Папка «' + name + '» создана', 'success');
                    refreshListing();
                } else {
                    showFolderError(result.data.message || 'Не удалось создать папку.');
                }
            }).catch(function () {
                showFolderError('Сетевая ошибка. Попробуйте ещё раз.');
            }).then(function () {
                if (folderSubmit) {
                    folderSubmit.disabled = false;
                }
            });
        });
    }

    function showFolderError(message) {
        if (!folderError) {
            toast(message, 'error');
            return;
        }
        folderError.textContent = message;
        folderError.hidden = false;
    }

    /* ---------- Uploader ---------- */
    var uploads = [];
    var activeUploads = 0;
    var uploadSeq = 0;
    var succeededSinceRefresh = 0;
    var refreshTimer = null;

    var uploadPanel = $('#upload-panel');
    var uploadList = $('#upload-list');
    var uploadCount = $('#upload-count');
    var uploadClear = $('#upload-clear');
    var uploadCollapse = $('#upload-collapse');
    var fileInput = $('#file-input');

    function activeItems() {
        return uploads.filter(function (item) {
            return item.status === 'queued' || item.status === 'uploading' || item.status === 'saving';
        });
    }

    function updateBadge() {
        var count = activeItems().length;
        if (uploadCount) {
            uploadCount.textContent = String(count);
        }
        if (uploadPanel) {
            uploadPanel.hidden = uploads.length === 0;
        }
    }

    function itemStatusText(item) {
        if (item.status === 'queued') {
            return 'В очереди';
        }
        if (item.status === 'uploading') {
            return Math.round(item.progress) + '%';
        }
        if (item.status === 'saving') {
            return 'Сохранение…';
        }
        if (item.status === 'done') {
            return 'Готово';
        }
        if (item.status === 'canceled') {
            return 'Отменено';
        }
        return item.error || 'Ошибка';
    }

    function createItemNode(item) {
        var node = document.createElement('div');
        node.className = 'upload-item queued';

        var top = document.createElement('div');
        top.className = 'upload-item-top';

        var name = document.createElement('span');
        name.className = 'upload-item-name';
        name.textContent = item.file.name;
        name.title = item.file.name;

        var status = document.createElement('span');
        status.className = 'upload-item-status';

        var cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'upload-cancel';
        cancel.textContent = '×';
        cancel.title = 'Отменить';
        cancel.addEventListener('click', function () {
            cancelUpload(item);
        });

        top.appendChild(name);
        top.appendChild(status);
        top.appendChild(cancel);

        var progress = document.createElement('div');
        progress.className = 'upload-progress';
        var fill = document.createElement('span');
        progress.appendChild(fill);

        node.appendChild(top);
        node.appendChild(progress);

        item.node = node;
        item.statusNode = status;
        item.fillNode = fill;
        item.cancelNode = cancel;
        if (uploadList) {
            uploadList.appendChild(node);
        }
        return node;
    }

    function renderItem(item) {
        if (!item.node) {
            createItemNode(item);
        }
        item.node.className = 'upload-item ' + item.status;
        if (item.statusNode) {
            item.statusNode.textContent = itemStatusText(item);
        }
        if (item.fillNode) {
            item.fillNode.style.width = Math.max(0, Math.min(100, item.progress)) + '%';
        }
        if (item.cancelNode) {
            item.cancelNode.hidden = item.status === 'done' || item.status === 'error' || item.status === 'canceled';
        }
        updateBadge();
    }

    function scheduleRefresh() {
        if (refreshTimer) {
            window.clearTimeout(refreshTimer);
        }
        refreshTimer = window.setTimeout(function () {
            refreshTimer = null;
            if (succeededSinceRefresh > 0) {
                var count = succeededSinceRefresh;
                succeededSinceRefresh = 0;
                toast('Загружено файлов: ' + count, 'success');
                refreshListing();
            }
        }, 800);
    }

    function finishUpload(item, status, errorMessage) {
        if (item.finished) {
            return;
        }
        item.finished = true;
        activeUploads = Math.max(0, activeUploads - 1);
        item.status = status;
        if (errorMessage) {
            item.error = errorMessage;
        }
        if (status === 'done') {
            item.progress = 100;
            succeededSinceRefresh += 1;
            scheduleRefresh();
        }
        renderItem(item);
        pumpQueue();
    }

    function postForm(url, params) {
        return fetch(url, {
            method: 'POST',
            headers: {
                'X-CSRFToken': getCsrfToken(),
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': 'application/json'
            },
            credentials: 'same-origin',
            body: params
        }).then(function (response) {
            return response.json().catch(function () {
                return {};
            }).then(function (data) {
                return { ok: response.ok, status: response.status, data: data };
            });
        });
    }

    function abortUploadSession(uploadId) {
        if (!uploadId) {
            return;
        }
        try {
            fetch(CONFIG.uploadAbortUrl, {
                method: 'POST',
                headers: {
                    'X-CSRFToken': getCsrfToken(),
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept': 'application/json'
                },
                credentials: 'same-origin',
                keepalive: true,
                body: new URLSearchParams({ upload_id: uploadId })
            });
        } catch (e) {}
    }

    function cancelUpload(item) {
        if (item.status === 'queued') {
            finishUpload(item, 'canceled');
            return;
        }
        if (item.status === 'uploading' && item.xhr) {
            item.xhr.abort();
            return;
        }
        if (item.uploadId) {
            abortUploadSession(item.uploadId);
        }
        if (item.status === 'uploading' || item.status === 'saving') {
            finishUpload(item, 'canceled');
        }
    }

    function startUpload(item) {
        item.status = 'uploading';
        activeUploads += 1;
        renderItem(item);

        var DEFAULT_CHUNK = 4 * 1024 * 1024;
        var MIN_CHUNK = 256 * 1024;
        var total = item.file.size;
        var chunkSize = DEFAULT_CHUNK;
        var uploadId = null;
        var offset = 0;

        function stopWithError(message) {
            if (item.finished) {
                return;
            }
            if (uploadId) {
                abortUploadSession(uploadId);
            }
            finishUpload(item, 'error', message);
            toast('«' + item.file.name + '»: ' + message, 'error');
        }

        function stopCanceled() {
            if (item.finished) {
                return;
            }
            if (uploadId) {
                abortUploadSession(uploadId);
            }
            finishUpload(item, 'canceled');
        }

        function completeUpload() {
            item.status = 'saving';
            item.progress = 100;
            renderItem(item);
            postForm(CONFIG.uploadCompleteUrl, new URLSearchParams({ upload_id: uploadId }))
                .then(function (result) {
                    if (item.finished) {
                        return;
                    }
                    if (result.ok && result.data.ok) {
                        finishUpload(item, 'done');
                    } else {
                        stopWithError(result.data.message || ('Не удалось сохранить файл (' + result.status + ')'));
                    }
                })
                .catch(function () {
                    stopWithError('Сетевая ошибка при сохранении файла');
                });
        }

        function sendChunk() {
            if (item.finished) {
                return;
            }
            if (offset >= total) {
                completeUpload();
                return;
            }

            var end = Math.min(offset + chunkSize, total);
            var blob = item.file.slice(offset, end);
            var xhr = new XMLHttpRequest();
            item.xhr = xhr;

            var form = new FormData();
            form.append('upload_id', uploadId);
            form.append('offset', String(offset));
            form.append('chunk', blob, 'chunk');

            xhr.open('POST', CONFIG.uploadChunkUrl, true);
            xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
            xhr.setRequestHeader('Accept', 'application/json');
            var csrf = getCsrfToken();
            if (csrf) {
                xhr.setRequestHeader('X-CSRFToken', csrf);
            }

            xhr.upload.onprogress = function (event) {
                if (!event.lengthComputable) {
                    return;
                }
                item.progress = ((offset + (event.loaded / event.total) * (end - offset)) / total) * 100;
                renderItem(item);
            };

            xhr.onload = function () {
                item.xhr = null;
                if (item.finished) {
                    return;
                }
                var data = null;
                try {
                    data = JSON.parse(xhr.responseText);
                } catch (e) {}

                if (xhr.status >= 200 && xhr.status < 300 && data && data.ok) {
                    offset = end;
                    item.progress = (offset / total) * 100;
                    renderItem(item);
                    sendChunk();
                    return;
                }

                if (xhr.status === 413 && chunkSize > MIN_CHUNK) {
                    chunkSize = Math.max(MIN_CHUNK, Math.floor(chunkSize / 2));
                    sendChunk();
                    return;
                }

                stopWithError((data && data.message) || ('Ошибка загрузки (' + xhr.status + ')'));
            };

            xhr.onerror = function () {
                item.xhr = null;
                stopWithError('Сетевая ошибка');
            };

            xhr.onabort = function () {
                item.xhr = null;
                stopCanceled();
            };

            xhr.send(form);
        }

        postForm(CONFIG.uploadInitUrl, new URLSearchParams({
            name: item.file.name,
            size: String(total),
            path: CONFIG.currentPath || '/',
            overwrite: 'true'
        })).then(function (result) {
            if (item.finished) {
                if (result.data && result.data.upload_id) {
                    abortUploadSession(result.data.upload_id);
                }
                return;
            }
            if (!result.ok || !result.data.ok) {
                throw new Error(result.data.message || ('Не удалось начать загрузку (' + result.status + ')'));
            }
            uploadId = result.data.upload_id;
            item.uploadId = uploadId;
            if (result.data.chunk_size) {
                chunkSize = result.data.chunk_size;
            }
            if (result.data.min_chunk_size) {
                MIN_CHUNK = result.data.min_chunk_size;
            }
            sendChunk();
        }).catch(function (error) {
            if (!item.finished) {
                stopWithError(error.message || 'Сетевая ошибка');
            }
        });
    }

    function pumpQueue() {
        while (activeUploads < MAX_CONCURRENT) {
            var next = uploads.find(function (item) {
                return item.status === 'queued';
            });
            if (!next) {
                break;
            }
            startUpload(next);
        }
        updateBadge();
    }

    function enqueue(files) {
        if (!CONFIG.canWrite || !files || files.length === 0) {
            return;
        }
        var added = 0;
        for (var i = 0; i < files.length; i += 1) {
            var file = files[i];
            if (!file || typeof file.size !== 'number') {
                continue;
            }
            uploads.push({
                id: ++uploadSeq,
                file: file,
                status: 'queued',
                progress: 0,
                finished: false,
                xhr: null,
                node: null,
                error: ''
            });
            added += 1;
        }
        if (added === 0) {
            return;
        }
        if (uploadPanel) {
            uploadPanel.hidden = false;
            uploadPanel.classList.remove('collapsed');
        }
        uploads.slice(-added).forEach(renderItem);
        pumpQueue();
    }

    ['#upload-button', '#upload-button-top', '#upload-button-content'].forEach(function (selector) {
        var button = $(selector);
        if (button && fileInput) {
            button.addEventListener('click', function () {
                fileInput.click();
            });
        }
    });

    if (fileInput) {
        fileInput.addEventListener('change', function () {
            enqueue(Array.prototype.slice.call(fileInput.files || []));
            fileInput.value = '';
        });
    }

    if (uploadClear) {
        uploadClear.addEventListener('click', function () {
            uploads = uploads.filter(function (item) {
                if (item.status === 'done' || item.status === 'error' || item.status === 'canceled') {
                    if (item.node) {
                        item.node.remove();
                    }
                    return false;
                }
                return true;
            });
            updateBadge();
        });
    }

    if (uploadCollapse) {
        uploadCollapse.addEventListener('click', function () {
            if (uploadPanel) {
                var collapsed = uploadPanel.classList.toggle('collapsed');
                uploadCollapse.textContent = collapsed ? '+' : '–';
            }
        });
    }

    if (uploadList) {
        uploadList.addEventListener('scroll', function () {});
    }

    /* ---------- Drag & drop ---------- */
    var dropOverlay = $('#drop-overlay');
    var dragDepth = 0;

    function hasFiles(event) {
        var types = event.dataTransfer && event.dataTransfer.types;
        if (!types) {
            return false;
        }
        return Array.prototype.indexOf.call(types, 'Files') !== -1;
    }

    if (dropOverlay) {
        ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(function (name) {
            window.addEventListener(name, function (event) {
                if (!CONFIG.canWrite) {
                    return;
                }
                if (name === 'dragover' && hasFiles(event)) {
                    event.preventDefault();
                    event.dataTransfer.dropEffect = 'copy';
                    return;
                }
                if (!hasFiles(event)) {
                    return;
                }
                event.preventDefault();
                if (name === 'dragenter') {
                    dragDepth += 1;
                    dropOverlay.classList.add('active');
                } else if (name === 'dragleave') {
                    dragDepth = Math.max(0, dragDepth - 1);
                    if (dragDepth === 0) {
                        dropOverlay.classList.remove('active');
                    }
                } else if (name === 'drop') {
                    dragDepth = 0;
                    dropOverlay.classList.remove('active');
                    var files = event.dataTransfer && event.dataTransfer.files;
                    if (files && files.length) {
                        enqueue(Array.prototype.slice.call(files));
                    }
                }
            });
        });
    }

    /* ---------- Forms UX ---------- */
    var urlForm = $('form.url-input-form');
    if (urlForm) {
        urlForm.addEventListener('submit', function () {
            var submit = urlForm.querySelector('button[type="submit"]');
            if (submit) {
                submit.disabled = true;
                submit.textContent = 'Открытие…';
            }
        });
        var urlInput = urlForm.querySelector('input[name="public_url"]');
        if (urlInput && !urlInput.value && navigator.clipboard && navigator.clipboard.readText) {
            navigator.clipboard.readText().then(function (text) {
                if (text && !urlInput.value && /disk\.yandex\.(ru|com)|yadi\.sk/i.test(text)) {
                    urlInput.value = text.trim();
                }
            }).catch(function () {});
        }
    }

    var zipForm = $('#zip-form');
    if (zipForm) {
        zipForm.addEventListener('submit', function () {
            if (selectedBoxes().filter(function (box) { return box.checked; }).length === 0) {
                return;
            }
            toast('Готовим ZIP-архив…', 'info');
        });
    }

    var downloadAllForm = $('.download-all-form');
    if (downloadAllForm) {
        downloadAllForm.addEventListener('submit', function () {
            toast('Готовим ZIP-архив всей папки…', 'info');
        });
    }

    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') {
            closeMediaModal();
            closeFolderModal();
            setSidebar(false);
            return;
        }
        if (!modalIsOpen() || galleryItems.length <= 1) {
            return;
        }
        var active = document.activeElement;
        if (active && (active.tagName === 'VIDEO' || active.tagName === 'AUDIO' || active.tagName === 'INPUT')) {
            return;
        }
        if (event.key === 'ArrowRight') {
            event.preventDefault();
            showGalleryItem(galleryIndex + 1);
        } else if (event.key === 'ArrowLeft') {
            event.preventDefault();
            showGalleryItem(galleryIndex - 1);
        }
    });

    initRegion();
})();
