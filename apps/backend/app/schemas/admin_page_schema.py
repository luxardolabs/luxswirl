"""
Paging limits shared by every admin-API list.
"""

# The highest `page` a list takes: past every real result set, and with the
# largest page size an offset the database still holds.
MAX_PAGE = 1_000_000
