"""Graphs package for AgriConnect."""

# NB : pas de `__all__` ici. Il déclarait autrefois `["nodes"]`, un
# sous-module qui n'existe pas (ni fichier ni paquet sous `graphs/`) —
# `from agriconnect.graphs import *` levait donc AttributeError. Les
# sous-paquets réels (`agents`, `factory`, `roles`, `state`) s'importent
# explicitement.
