"""Keep the old generator import working."""

from generate_data import generate_dataset


def generate_flexible_dataset(output, *, orders=150, **options):
    return generate_dataset(output, order_count=orders, **options)
