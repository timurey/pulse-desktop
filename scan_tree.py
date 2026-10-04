#!/usr/bin/env python3
"""
Иерархия сканов проекта: группы произвольной глубины (здание, блок, этаж,
комната, снаружи...) и сканы-листья. На геометрию не влияет — только
организация, видимость веток и выбор подмножеств (экспорт ветки, стыковка по дереву).

Формат в project.json — поле `tree`:
  группа: {"id", "name", "kind", "visible", "children": [...]}
  лист:   {"scan": <id скана>, "visible": bool}
Каждый скан входит ровно в одну группу. Проект без `tree` открывается так,
будто все сканы лежат в корне.
"""

import itertools

KINDS = ['здание', 'блок', 'этаж', 'комната', 'снаружи', 'прочее']


class Group:
    def __init__(self, gid, name, kind='прочее', visible=True):
        self.id, self.name, self.kind, self.visible = gid, name, kind, visible
        self.children = []          # Group | Leaf

    def to_json(self):
        return {'id': self.id, 'name': self.name, 'kind': self.kind, 'visible': self.visible,
                'children': [c.to_json() for c in self.children]}


class Leaf:
    def __init__(self, scan_id, visible=True):
        self.scan, self.visible = scan_id, visible

    def to_json(self):
        return {'scan': self.scan, 'visible': self.visible}


class Tree:
    def __init__(self):
        self.root = Group('root', 'Проект', 'проект')
        self._next = 1

    # ── загрузка / сохранение ─────────────────────────────────────────────
    @classmethod
    def from_json(cls, data, scan_ids=()):
        t = cls()
        if data:
            def build(d):
                if 'scan' in d:
                    return Leaf(d['scan'], d.get('visible', True))
                g = Group(d['id'], d.get('name', d['id']), d.get('kind', 'прочее'),
                          d.get('visible', True))
                g.children = [build(c) for c in d.get('children', [])]
                return g
            t.root = build(data)
            nums = [int(g.id[1:]) for g in t.groups() if g.id[:1] == 'g' and g.id[1:].isdigit()]
            t._next = max(nums, default=0) + 1
        t.sync(scan_ids)
        return t

    def to_json(self):
        return self.root.to_json()

    def sync(self, scan_ids):
        """Добавить в корень сканы, которых нет в дереве; убрать ссылки на удалённые."""
        ids = list(scan_ids)
        known = set(ids)

        def prune(g):
            g.children = [c for c in g.children if not (isinstance(c, Leaf) and c.scan not in known)]
            for c in g.children:
                if isinstance(c, Group):
                    prune(c)
        prune(self.root)
        have = {l.scan for l in self.leaves()}
        for sid in ids:
            if sid not in have:
                self.root.children.append(Leaf(sid))

    # ── обход ────────────────────────────────────────────────────────────
    def groups(self, g=None):
        g = g or self.root
        yield g
        for c in g.children:
            if isinstance(c, Group):
                yield from self.groups(c)

    def leaves(self, g=None):
        g = g or self.root
        for c in g.children:
            if isinstance(c, Leaf):
                yield c
            else:
                yield from self.leaves(c)

    def scans_in(self, gid):
        """Id сканов ветки (группы) или [id] для скана."""
        g = self.group(gid)
        if g is None:
            return [gid] if self.leaf(gid) else []
        return [l.scan for l in self.leaves(g)]

    def group(self, gid):
        return next((g for g in self.groups() if g.id == gid), None)

    def leaf(self, scan_id):
        return next((l for l in self.leaves() if l.scan == scan_id), None)

    def node(self, key):
        return self.group(key) or self.leaf(key)

    def parent(self, key):
        """Группа-родитель узла (группы по id или скана по id)."""
        for g in self.groups():
            for c in g.children:
                if (isinstance(c, Group) and c.id == key) or (isinstance(c, Leaf) and c.scan == key):
                    return g
        return None

    def ancestors(self, key):
        out = []
        p = self.parent(key)
        while p is not None:
            out.append(p)
            p = self.parent(p.id) if p is not self.root else None
        return out

    def path(self, key):
        """«Здание А / 1 этаж / Комната 101» для узла."""
        names = [g.name for g in reversed(self.ancestors(key)) if g is not self.root]
        n = self.node(key)
        if isinstance(n, Group) and n is not self.root:
            names.append(n.name)
        return ' / '.join(names)

    def group_choices(self):
        """[(id, подпись с отступом)] для списков выбора группы."""
        out = []

        def walk(g, depth):
            out.append((g.id, '  ' * depth + (g.name if g is not self.root else 'Проект (корень)')))
            for c in g.children:
                if isinstance(c, Group):
                    walk(c, depth + 1)
        walk(self.root, 0)
        return out

    # ── видимость ────────────────────────────────────────────────────────
    def effective_visible(self, scan_id):
        """Скан виден, если видим он сам и все его предки."""
        l = self.leaf(scan_id)
        if l is None:
            return True
        return l.visible and all(g.visible for g in self.ancestors(scan_id))

    def set_visible(self, key, visible):
        n = self.node(key)
        if n is not None:
            n.visible = visible

    def show_all(self):
        for g in self.groups():
            g.visible = True
        for l in self.leaves():
            l.visible = True

    def only(self, keys):
        """Показать только эти узлы (группы целиком, сканы) — остальное скрыть."""
        keep = set()
        for k in keys:
            keep.update(self.scans_in(k))
        self.show_all()
        for l in self.leaves():
            l.visible = l.scan in keep

    # ── правка ───────────────────────────────────────────────────────────
    def add_group(self, parent_id, name, kind='прочее'):
        p = self.group(parent_id) or self.root
        gid = f'g{self._next}'
        self._next += 1
        g = Group(gid, name or f'Группа {gid[1:]}', kind)
        p.children.append(g)
        return gid

    def rename(self, gid, name=None, kind=None):
        g = self.group(gid)
        if g is None or g is self.root:
            return False
        if name:
            g.name = name
        if kind:
            g.kind = kind
        return True

    def delete_group(self, gid):
        """Удалить группу; её дети переходят к родителю."""
        g = self.group(gid)
        if g is None or g is self.root:
            return False
        p = self.parent(gid)
        i = p.children.index(g)
        p.children[i:i + 1] = g.children
        return True

    def move(self, key, target_gid, index=None):
        """
        Переместить скан или группу в группу target на позицию index (None — в конец).
        index — позиция среди детей target до перемещения (как у отметки вставки в дереве).
        Защита от циклов.
        """
        return self.move_many([key], target_gid, index)

    def _can_move(self, n, target):
        if n is None or n is self.root or target is None:
            return False
        if isinstance(n, Group) and (n is target or any(g is target for g in self.groups(n))):
            return False                       # нельзя переместить группу внутрь себя
        return True

    def order(self):
        """Ключи всех узлов в порядке обхода дерева (как в списке)."""
        out = []

        def walk(g):
            for c in g.children:
                out.append(c.id if isinstance(c, Group) else c.scan)
                if isinstance(c, Group):
                    walk(c)
        walk(self.root)
        return out

    def move_many(self, keys, target_gid, index=None):
        """
        Переместить несколько узлов в группу target, начиная с позиции index, сохраняя их
        порядок в дереве. Узлы, чей предок тоже перемещается, едут вместе с ним.
        → True, если что-то перемещено.
        """
        target = self.group(target_gid)
        if target is None:
            return False
        keyset = set(keys)
        rank = {k: i for i, k in enumerate(self.order())}
        nodes = []
        for k in sorted(set(keys), key=lambda k: rank.get(k, 1 << 30)):
            n = self.node(k)
            if any((a.id in keyset) for a in self.ancestors(k)):
                continue                       # едет вместе с предком
            if not self._can_move(n, target):
                return False
            nodes.append(n)
        if not nodes:
            return False
        if index is None:
            index = len(target.children)
        # позиция index — в списке детей target до удаления перемещаемых
        shift = sum(1 for c in target.children[:index] if c in nodes)
        for n in nodes:
            self.parent(n.id if isinstance(n, Group) else n.scan).children.remove(n)
        index = max(0, min(len(target.children), index - shift))
        target.children[index:index] = nodes
        return True

    def group_from(self, keys, name, kind='прочее'):
        """
        Новая группа из выбранных узлов: создаётся в родителе первого из них, на его месте,
        и узлы переносятся в неё (в порядке дерева). → id группы или None.
        """
        rank = {k: i for i, k in enumerate(self.order())}
        keys = [k for k in sorted(set(keys), key=lambda k: rank.get(k, 1 << 30)) if self.node(k) is not None]
        if not keys:
            return None
        parent = self.parent(keys[0]) or self.root
        pos = parent.children.index(self.node(keys[0]))
        gid = self.add_group(parent.id, name, kind)
        g = self.group(gid)
        parent.children.remove(g)
        parent.children.insert(pos, g)
        if not self.move_many(keys, gid):
            parent.children.remove(g)
            return None
        return gid

    # ── стыковка по дереву ───────────────────────────────────────────────
    def registration_pairs(self, scan_ids, weight=None, reps=2):
        """
        Пары сканов для автостыковки по дереву (вместо всех N(N-1)/2):
          - все пары сканов внутри одной группы (непосредственные листья);
          - между соседними узлами одного родителя — пары их «представителей»
            (reps самых крупных сканов каждой ветки по weight).
        Порядок в паре — как в scan_ids.
        """
        order = {s: i for i, s in enumerate(scan_ids)}
        weight = weight or {}
        pairs = set()

        def reps_of(node):
            ids = [node.scan] if isinstance(node, Leaf) else [l.scan for l in self.leaves(node)]
            ids = [s for s in ids if s in order]
            return sorted(ids, key=lambda s: -weight.get(s, 0))[:reps]

        def add(a, b):
            if a != b and a in order and b in order:
                pairs.add(tuple(sorted((a, b), key=order.get)))

        for g in self.groups():
            leaves = [c.scan for c in g.children if isinstance(c, Leaf)]
            for a, b in itertools.combinations(leaves, 2):
                add(a, b)
            kids = list(g.children)
            for x, y in itertools.combinations(kids, 2):
                if isinstance(x, Leaf) and isinstance(y, Leaf):
                    continue                   # уже учтены выше
                for a in reps_of(x):
                    for b in reps_of(y):
                        add(a, b)
        return sorted(pairs, key=lambda p: (order[p[0]], order[p[1]]))

    def is_flat(self):
        return not any(isinstance(c, Group) for c in self.root.children)
